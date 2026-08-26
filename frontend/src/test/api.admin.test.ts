import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  issueAdminInvitation,
  listAdminRoles,
  replaceAdminMemberRoles,
} from '../api';

function successResponse(data: unknown, status = 200): Response {
  return new Response(
    JSON.stringify({
      success: true,
      data,
      meta: {
        request_id: 'request-test',
        query_time_ms: 1,
        dimensions: [],
        metrics: [],
      },
      error: null,
    }),
    {
      status,
      headers: { 'Content-Type': 'application/json', 'X-Request-Id': 'request-test' },
    },
  );
}

describe('administration API client', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    document.cookie = 'medical_ai_csrf=csrf-proof; Path=/';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('forwards the role cursor and parses the bounded page', async () => {
    fetchMock.mockResolvedValue(successResponse({ items: [], next_cursor: null }));

    await expect(listAdminRoles('role cursor')).resolves.toEqual({
      items: [],
      next_cursor: null,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/admin/roles?cursor=role%20cursor',
      expect.objectContaining({ credentials: 'same-origin' }),
    );
  });

  it('sends optimistic role updates with the in-memory CSRF proof', async () => {
    const member = {
      membership_id: 'member-1',
      user_id: 'user-1',
      email: 'analyst@example.com',
      display_name: 'Analyst',
      user_status: 'active',
      membership_status: 'active',
      authorization_version: 2,
      version: 2,
      roles: [],
      facilities: [],
    };
    fetchMock.mockResolvedValue(successResponse(member));

    await replaceAdminMemberRoles('member-1', ['role-1'], 1);

    const [, options] = fetchMock.mock.calls[0];
    expect(options?.method).toBe('PUT');
    expect(JSON.parse(String(options?.body))).toEqual({
      role_ids: ['role-1'],
      expected_version: 1,
    });
    expect(new Headers(options?.headers).get('X-CSRF-Token')).toBe('csrf-proof');
  });

  it('accepts the one-time invitation token contract', async () => {
    fetchMock.mockResolvedValue(successResponse({
      membership_id: 'member-new',
      token: 'one-time-token',
      expires_at: '2026-08-26T00:00:00Z',
    }, 201));

    await expect(issueAdminInvitation('new@example.com', 24)).resolves.toMatchObject({
      token: 'one-time-token',
      membership_id: 'member-new',
    });
  });
});
