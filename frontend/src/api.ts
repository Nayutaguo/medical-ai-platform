import type {
  AnalysisHistoryItem,
  AskPayload,
  AdminPermission,
  AdminFacility,
  AdminListPayload,
  AdminMember,
  AdminRole,
  AuthSession,
  AuditEvent,
  CurrentSessionPayload,
  DistinctPayload,
  FacilitySyncPayload,
  HealthPayload,
  LoginCredentials,
  LoginPayload,
  LogoutPayload,
  PasswordChangePayload,
  PasswordResetPayload,
  PasswordResetRequestPayload,
  QueryResult,
  RegistrationCredentials,
  RegistrationPayload,
  SchemaPayload,
  IssuedInvitation,
} from './types';

const API_PREFIX = '/api/v1';
const DEFAULT_CSRF_HEADER_NAME = 'X-CSRF-Token';
const DEFAULT_CSRF_COOKIE_NAME = 'medical_ai_csrf';

let csrfToken: string | null = null;
let csrfHeaderName = DEFAULT_CSRF_HEADER_NAME;
let csrfCookieName = DEFAULT_CSRF_COOKIE_NAME;
let anonymousDevelopmentMode = false;

interface ApiErrorPayload {
  code: string;
  message: string;
  field_errors?: Array<{ field: string; message: string }>;
}

interface ApiEnvelope<T> {
  success: boolean;
  data: T | null;
  meta: {
    request_id: string;
    query_time_ms: number;
    dimensions: string[];
    metrics: string[];
  };
  error: ApiErrorPayload | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly fieldErrors: Array<{ field: string; message: string }>;

  constructor({
    status,
    code,
    message,
    requestId,
    fieldErrors = [],
  }: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
    fieldErrors?: Array<{ field: string; message: string }>;
  }) {
    const suffix = requestId ? `（request_id: ${requestId}）` : '';
    super(`${message}${suffix}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId ?? null;
    this.fieldErrors = fieldErrors;
  }
}

async function requestJson<T>(path: string, options?: RequestInit, includeCsrf = false): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (includeCsrf) {
    if (!anonymousDevelopmentMode) {
      csrfToken ??= readCookie(csrfCookieName);
      if (!csrfToken) {
        throw new ApiError({
          status: 403,
          code: 'CSRF_TOKEN_MISSING',
          message: '当前会话的安全凭据已丢失，请重新登录',
        });
      }
      headers.set(csrfHeaderName, csrfToken);
    }
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      headers,
    });
  } catch {
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: '无法连接服务，请检查网络后重试',
    });
  }
  const rawBody = await response.text();
  const headerRequestId = response.headers.get('X-Request-Id');
  if (!rawBody.trim()) {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: `服务返回了空响应（HTTP ${response.status}）`,
      requestId: headerRequestId,
    });
  }

  const contentType = response.headers.get('Content-Type')?.toLowerCase() ?? '';
  if (contentType && !contentType.includes('json')) {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: `服务返回了非 JSON 响应（HTTP ${response.status}）`,
      requestId: headerRequestId,
    });
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(rawBody);
  } catch {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: `服务返回了无法解析的 JSON（HTTP ${response.status}）`,
      requestId: headerRequestId,
    });
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: `服务返回了无效的响应结构（HTTP ${response.status}）`,
      requestId: headerRequestId,
    });
  }
  const payload = parsed as ApiEnvelope<T>;

  if (!response.ok || !payload.success || payload.data === null) {
    throw new ApiError({
      status: response.status,
      code: payload.error?.code ?? 'REQUEST_FAILED',
      message: payload.error?.message ?? `Request failed with ${response.status}`,
      requestId: payload.meta?.request_id ?? headerRequestId,
      fieldErrors: payload.error?.field_errors,
    });
  }
  return payload.data;
}

/** Return true only for an explicit, safe anonymous-development signal. */
export function isAuthenticationDisabled(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.code === 'AUTHENTICATION_DISABLED' || error.code === 'AUTH_NOT_ENABLED')
  );
}

/** Treat a rejected session and a lost CSRF proof as the same local auth boundary. */
export function isAuthenticationLost(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 401 || error.code === 'CSRF_TOKEN_MISSING')
  );
}

export function login(credentials: LoginCredentials): Promise<AuthSession> {
  const organizationId = credentials.organization_id?.trim();
  return requestJson<LoginPayload>(`${API_PREFIX}/auth/sessions`, {
    method: 'POST',
    body: JSON.stringify({
      email: credentials.email,
      password: credentials.password,
      ...(organizationId ? { organization_id: organizationId } : {}),
    }),
  }).then((payload) => {
    anonymousDevelopmentMode = false;
    applyCsrfConfiguration(payload);
    csrfToken = payload.csrf_token;
    return payload.session;
  });
}

/** Register an account from a one-time invitation without retaining credentials. */
export function registerAccount(credentials: RegistrationCredentials): Promise<RegistrationPayload> {
  return requestJson<RegistrationPayload>(`${API_PREFIX}/auth/registrations`, {
    method: 'POST',
    body: JSON.stringify({
      invitation_token: credentials.invitation_token.trim(),
      email: credentials.email.trim(),
      display_name: credentials.display_name.trim(),
      password: credentials.password,
    }),
  });
}

export async function getCurrentSession(): Promise<AuthSession> {
  try {
    const payload = await requestJson<CurrentSessionPayload>(`${API_PREFIX}/auth/me`);
    anonymousDevelopmentMode = false;
    applyCsrfConfiguration(payload);
    // The server owns the cookie. We only copy its proof into module memory so
    // a restored session can make CSRF-protected requests without web storage.
    csrfToken = readCookie(csrfCookieName);
    return payload.session;
  } catch (error) {
    if (isAuthenticationDisabled(error)) {
      anonymousDevelopmentMode = true;
      csrfToken = null;
    }
    throw error;
  }
}

function applyCsrfConfiguration(configuration: {
  csrf_cookie_name: string;
  csrf_header_name: string;
}): void {
  csrfCookieName = configuration.csrf_cookie_name || DEFAULT_CSRF_COOKIE_NAME;
  csrfHeaderName = configuration.csrf_header_name || DEFAULT_CSRF_HEADER_NAME;
}

export function logout(): Promise<void> {
  return requestJson<LogoutPayload>(
    `${API_PREFIX}/auth/sessions/current`,
    { method: 'DELETE' },
    true,
  ).then(() => {
    anonymousDevelopmentMode = false;
    csrfToken = null;
  });
}

export function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  return requestJson<PasswordChangePayload>(
    `${API_PREFIX}/auth/password/change`,
    {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    },
    true,
  ).then(() => {
    csrfToken = null;
  });
}

export function requestPasswordReset(
  email: string,
  organizationId?: string,
): Promise<PasswordResetRequestPayload> {
  const normalizedOrganization = organizationId?.trim();
  return requestJson<PasswordResetRequestPayload>(`${API_PREFIX}/auth/password-reset-requests`, {
    method: 'POST',
    body: JSON.stringify({
      email: email.trim(),
      ...(normalizedOrganization ? { organization_id: normalizedOrganization } : {}),
    }),
  });
}

export function resetPassword(
  token: string,
  email: string,
  organizationId: string,
  newPassword: string,
): Promise<PasswordResetPayload> {
  return requestJson<PasswordResetPayload>(`${API_PREFIX}/auth/password-resets`, {
    method: 'POST',
    body: JSON.stringify({
      token: token.trim(),
      email: email.trim(),
      organization_id: organizationId.trim(),
      new_password: newPassword,
    }),
  });
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null;
  }
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(';')) {
    const candidate = part.trim();
    if (candidate.startsWith(prefix)) {
      try {
        return decodeURIComponent(candidate.slice(prefix.length)) || null;
      } catch {
        return null;
      }
    }
  }
  return null;
}

export function getHealth(): Promise<HealthPayload> {
  return requestJson<HealthPayload>(`${API_PREFIX}/health`);
}

export function getSchema(): Promise<SchemaPayload> {
  return requestJson<SchemaPayload>(`${API_PREFIX}/schema`);
}

export function getDistinct(field: string): Promise<DistinctPayload> {
  return requestJson<DistinctPayload>(`${API_PREFIX}/distinct?table=inpatient&field=${encodeURIComponent(field)}&limit=30`);
}

export function ask(question: string): Promise<AskPayload> {
  return requestJson<AskPayload>(
    `${API_PREFIX}/ask`,
    {
      method: 'POST',
      body: JSON.stringify({ question, execute: true }),
    },
    true,
  );
}

export async function runQuerySpec(querySpec: Record<string, unknown>): Promise<QueryResult> {
  const payload = await requestJson<{ result: QueryResult }>(
    `${API_PREFIX}/query`,
    {
      method: 'POST',
      body: JSON.stringify({ query_spec: querySpec }),
    },
    true,
  );
  return payload.result;
}

export function listAdminMembers(cursor?: string): Promise<AdminListPayload<AdminMember>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return requestJson<AdminListPayload<AdminMember>>(`${API_PREFIX}/admin/members${query}`);
}

export function listAdminRoles(cursor?: string): Promise<AdminListPayload<AdminRole>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return requestJson<AdminListPayload<AdminRole>>(`${API_PREFIX}/admin/roles${query}`);
}

export function listAdminPermissions(cursor?: string): Promise<AdminListPayload<AdminPermission>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return requestJson<AdminListPayload<AdminPermission>>(`${API_PREFIX}/admin/permissions${query}`);
}

export function createAdminRole(input: {
  role_key: string;
  name: string;
  description: string;
  permission_ids: string[];
}): Promise<AdminRole> {
  return requestJson<AdminRole>(
    `${API_PREFIX}/admin/roles`,
    { method: 'POST', body: JSON.stringify(input) },
    true,
  );
}

export function updateAdminRole(
  roleId: string,
  input: { name: string; description: string; permission_ids: string[]; expected_version: number },
): Promise<AdminRole> {
  return requestJson<AdminRole>(
    `${API_PREFIX}/admin/roles/${encodeURIComponent(roleId)}`,
    { method: 'PUT', body: JSON.stringify(input) },
    true,
  );
}

export function deleteAdminRole(roleId: string, expectedVersion: number): Promise<void> {
  return requestJson<{ deleted: true }>(
    `${API_PREFIX}/admin/roles/${encodeURIComponent(roleId)}`,
    {
      method: 'DELETE',
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
    true,
  ).then(() => undefined);
}

export function listAdminFacilities(cursor?: string): Promise<AdminListPayload<AdminFacility>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return requestJson<AdminListPayload<AdminFacility>>(`${API_PREFIX}/admin/facilities${query}`);
}

export function issueAdminInvitation(email: string, lifetimeHours = 24): Promise<IssuedInvitation> {
  return requestJson<IssuedInvitation>(
    `${API_PREFIX}/admin/invitations`,
    {
      method: 'POST',
      body: JSON.stringify({ email: email.trim(), lifetime_hours: lifetimeHours }),
    },
    true,
  );
}

export function replaceAdminMemberRoles(
  membershipId: string,
  roleIds: string[],
  expectedVersion: number,
): Promise<AdminMember> {
  return requestJson<AdminMember>(
    `${API_PREFIX}/admin/members/${encodeURIComponent(membershipId)}/roles`,
    {
      method: 'PUT',
      body: JSON.stringify({ role_ids: roleIds, expected_version: expectedVersion }),
    },
    true,
  );
}

export function replaceAdminMemberFacilityScope(
  membershipId: string,
  facilityIds: string[],
  expectedVersion: number,
): Promise<AdminMember> {
  return requestJson<AdminMember>(
    `${API_PREFIX}/admin/members/${encodeURIComponent(membershipId)}/facility-scope`,
    {
      method: 'PUT',
      body: JSON.stringify({ facility_ids: facilityIds, expected_version: expectedVersion }),
    },
    true,
  );
}

export function updateAdminMemberStatus(
  membershipId: string,
  status: 'active' | 'suspended',
  expectedVersion: number,
): Promise<AdminMember> {
  return requestJson<AdminMember>(
    `${API_PREFIX}/admin/members/${encodeURIComponent(membershipId)}/status`,
    {
      method: 'PATCH',
      body: JSON.stringify({ status, expected_version: expectedVersion }),
    },
    true,
  );
}

export function syncAdminFacilities(): Promise<FacilitySyncPayload> {
  return requestJson<FacilitySyncPayload>(
    `${API_PREFIX}/admin/facilities/sync`,
    { method: 'POST', body: JSON.stringify({}) },
    true,
  );
}

export function listAuditEvents(
  cursor?: string,
  filters: { action?: string; outcome?: string } = {},
): Promise<AdminListPayload<AuditEvent>> {
  const params = new URLSearchParams();
  if (cursor) params.set('cursor', cursor);
  if (filters.action?.trim()) params.set('action', filters.action.trim());
  if (filters.outcome?.trim()) params.set('outcome', filters.outcome.trim());
  const query = params.size ? `?${params.toString()}` : '';
  return requestJson<AdminListPayload<AuditEvent>>(`${API_PREFIX}/admin/audit-events${query}`);
}

export function listAnalysisHistory(
  cursor?: string,
  favoriteOnly = false,
): Promise<AdminListPayload<AnalysisHistoryItem>> {
  const params = new URLSearchParams();
  if (cursor) params.set('cursor', cursor);
  if (favoriteOnly) params.set('favorite', 'true');
  const query = params.size ? `?${params.toString()}` : '';
  return requestJson<AdminListPayload<AnalysisHistoryItem>>(`${API_PREFIX}/history${query}`);
}

export function updateHistoryFavorite(
  historyId: string,
  favorite: boolean,
  version: number,
): Promise<AnalysisHistoryItem> {
  return requestJson<AnalysisHistoryItem>(
    `${API_PREFIX}/history/${encodeURIComponent(historyId)}/favorite`,
    {
      method: 'PATCH',
      body: JSON.stringify({ is_favorite: favorite, version }),
    },
    true,
  );
}

export function deleteAnalysisHistory(historyId: string): Promise<void> {
  return requestJson<{ deleted: true }>(
    `${API_PREFIX}/history/${encodeURIComponent(historyId)}`,
    { method: 'DELETE' },
    true,
  ).then(() => undefined);
}
