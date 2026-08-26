import type {
  AskPayload,
  AdminFacility,
  AdminListPayload,
  AdminMember,
  AdminRole,
  AuthSession,
  CurrentSessionPayload,
  DistinctPayload,
  FacilitySyncPayload,
  HealthPayload,
  LoginCredentials,
  LoginPayload,
  LogoutPayload,
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
