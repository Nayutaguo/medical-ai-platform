import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getCurrentSession, replaceAdminMemberRoles } from '../api';

function successResponse(data: unknown): Response {
  return new Response(
    JSON.stringify({
      success: true,
      data,
      meta: {
        request_id: 'request-auth',
        query_time_ms: 1,
        dimensions: [],
        metrics: [],
      },
      error: null,
    }),
    {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    },
  );
}

const session = {
  user: { id: 'user-1', email: 'admin@example.com', display_name: 'Admin' },
  organization: { id: 'org-1', name: 'Hospital', membership_id: 'member-1' },
  permissions: ['identity.roles.assign'],
  expires_at: '2026-08-27T00:00:00Z',
  idle_expires_at: '2026-08-26T12:30:00Z',
};

describe('authentication API client configuration', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    document.cookie = 'custom_csrf=custom-proof; Path=/';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses the CSRF cookie and header names advertised by the server', async () => {
    fetchMock
      .mockResolvedValueOnce(successResponse({
        session,
        csrf_cookie_name: 'custom_csrf',
        csrf_header_name: 'X-Custom-CSRF',
      }))
      .mockResolvedValueOnce(successResponse({
        membership_id: 'member-1',
        user_id: 'user-1',
        email: 'admin@example.com',
        display_name: 'Admin',
        user_status: 'active',
        membership_status: 'active',
        authorization_version: 2,
        version: 2,
        roles: [],
        facilities: [],
      }));

    await getCurrentSession();
    await replaceAdminMemberRoles('member-1', [], 1);

    const [, options] = fetchMock.mock.calls[1];
    expect(new Headers(options?.headers).get('X-Custom-CSRF')).toBe('custom-proof');
    expect(new Headers(options?.headers).has('X-CSRF-Token')).toBe(false);
  });
});
