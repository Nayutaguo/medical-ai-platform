import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  changePassword,
  createAdminRole,
  listAuditEvents,
  listAnalysisHistory,
  requestPasswordReset,
  updateHistoryFavorite,
} from '../api';

function successResponse(data: unknown, status = 200): Response {
  return new Response(
    JSON.stringify({
      success: true,
      data,
      meta: { request_id: 'request-completion', query_time_ms: 1, dimensions: [], metrics: [] },
      error: null,
    }),
    { status, headers: { 'Content-Type': 'application/json' } },
  );
}

describe('demo-completion API clients', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    document.cookie = 'medical_ai_csrf=completion-proof; Path=/';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('writes a custom role with permission identifiers and CSRF', async () => {
    fetchMock.mockResolvedValue(successResponse({
      id: 'role-1', role_key: 'viewer', name: 'Viewer', description: '', permissions: [], is_system: false, version: 1,
    }, 201));

    await createAdminRole({ role_key: 'viewer', name: 'Viewer', description: '', permission_ids: ['permission-1'] });

    const [path, options] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/v1/admin/roles');
    expect(options?.method).toBe('POST');
    expect(JSON.parse(String(options?.body))).toMatchObject({ permission_ids: ['permission-1'] });
    expect(new Headers(options?.headers).get('X-CSRF-Token')).toBe('completion-proof');
  });

  it('lists and favorites membership-scoped history', async () => {
    const item = {
      id: '9', kind: 'query', title: 'query', question: null, query_spec: { table: 'inpatient' }, chart_spec: null,
      row_count: 2, truncated: false, query_time_ms: 3, is_favorite: true, created_at: '2026-08-26T00:00:00Z', updated_at: '2026-08-26T00:00:00Z', version: 2,
    };
    fetchMock
      .mockResolvedValueOnce(successResponse({ items: [item], next_cursor: null }))
      .mockResolvedValueOnce(successResponse(item));

    await listAnalysisHistory(undefined, true);
    await updateHistoryFavorite('9', true, 1);

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/history?favorite=true');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/history/9/favorite');
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ is_favorite: true, version: 1 });
  });

  it('uses non-enumerating reset request and authenticated password change endpoints', async () => {
    fetchMock
      .mockResolvedValueOnce(successResponse({ accepted: true }))
      .mockResolvedValueOnce(successResponse({ changed: true }));

    await requestPasswordReset('user@example.com', 'org-1');
    await changePassword('old-password', 'new-password-value');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/password-reset-requests');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/auth/password/change');
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('X-CSRF-Token')).toBe('completion-proof');
  });

  it('forwards safe audit filters', async () => {
    fetchMock.mockResolvedValue(successResponse({ items: [], next_cursor: null }));
    await listAuditEvents('41', { action: 'identity.role.create', outcome: 'success' });
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/admin/audit-events?cursor=41&action=identity.role.create&outcome=success',
    );
  });
});
