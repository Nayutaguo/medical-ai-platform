import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import {
  ApiError,
  getCurrentSession,
  issueAdminInvitation,
  listAdminFacilities,
  listAdminMembers,
  listAdminPermissions,
  listAdminRoles,
  replaceAdminMemberFacilityScope,
  replaceAdminMemberRoles,
  updateAdminMemberStatus,
} from '../api';
import type { AdminFacility, AdminMember, AdminRole, AuthSession } from '../types';

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    getCurrentSession: vi.fn(),
    getHealth: vi.fn(),
    getSchema: vi.fn(),
    listAdminMembers: vi.fn(),
    listAdminPermissions: vi.fn(),
    listAdminRoles: vi.fn(),
    listAdminFacilities: vi.fn(),
    issueAdminInvitation: vi.fn(),
    replaceAdminMemberRoles: vi.fn(),
    replaceAdminMemberFacilityScope: vi.fn(),
    updateAdminMemberStatus: vi.fn(),
    syncAdminFacilities: vi.fn(),
  };
});

const session: AuthSession = {
  user: { id: 'user-admin', email: 'admin@example.com', display_name: 'Admin User' },
  organization: { id: 'org-1', name: 'Hospital A', membership_id: 'member-admin' },
  permissions: ['users.manage', 'roles.assign', 'imports.create', 'audit.read'],
  expires_at: '2099-08-26T00:00:00Z',
  idle_expires_at: '2099-08-25T23:00:00Z',
};

const analystRole: AdminRole = {
  id: 'role-analyst',
  role_key: 'data_analyst',
  name: 'Data Analyst',
  description: 'Governed analytics access',
  permissions: [
    'analytics.schema.read',
    'analytics.distinct.read',
    'analytics.query.execute',
    'analytics.agent.execute',
  ],
  version: 1,
};

const administratorRole: AdminRole = {
  id: 'role-admin',
  role_key: 'organization_admin',
  name: 'Organization Admin',
  description: 'Organization governance',
  permissions: ['users.manage', 'roles.assign', 'imports.create', 'audit.read'],
  version: 1,
};

const facility: AdminFacility = {
  id: 'facility-1',
  facility_key: '1001',
  display_name: 'Example Medical Center',
  status: 'active',
  version: 1,
};

const analystMember: AdminMember = {
  membership_id: 'member-analyst',
  user_id: 'user-analyst',
  email: 'analyst@example.com',
  display_name: 'Analyst User',
  user_status: 'active',
  membership_status: 'active',
  authorization_version: 1,
  version: 1,
  roles: [],
  facilities: [],
};

function configureBaseMocks() {
  vi.mocked(getCurrentSession).mockResolvedValue(session);
  vi.mocked(listAdminMembers).mockResolvedValue({ items: [analystMember], next_cursor: null });
  vi.mocked(listAdminPermissions).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(listAdminRoles).mockResolvedValue({ items: [analystRole], next_cursor: null });
  vi.mocked(listAdminFacilities).mockResolvedValue({ items: [facility], next_cursor: null });
}

describe('administration workspace', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.clearAllMocks();
    configureBaseMocks();
  });

  it('updates roles then facility scope using the returned optimistic version', async () => {
    const user = userEvent.setup();
    const withRole: AdminMember = {
      ...analystMember,
      roles: [analystRole],
      authorization_version: 2,
      version: 2,
    };
    const withScope: AdminMember = {
      ...withRole,
      facilities: [facility],
      authorization_version: 3,
      version: 3,
    };
    vi.mocked(replaceAdminMemberRoles).mockResolvedValue(withRole);
    vi.mocked(replaceAdminMemberFacilityScope).mockResolvedValue(withScope);

    render(<App />);

    expect(await screen.findByText('组织管理')).toBeInTheDocument();
    const memberRow = (await screen.findByText('Analyst User')).closest('tr');
    expect(memberRow).not.toBeNull();
    await user.click(within(memberRow as HTMLTableRowElement).getByRole('button', { name: '配置权限' }));
    await user.click(screen.getByRole('checkbox', { name: 'Data Analyst' }));
    await user.click(screen.getByRole('checkbox', { name: /Example Medical Center/ }));
    await user.click(screen.getByRole('button', { name: '保存权限' }));

    await waitFor(() => {
      expect(replaceAdminMemberRoles).toHaveBeenCalledWith(
        'member-analyst',
        ['role-analyst'],
        1,
      );
      expect(replaceAdminMemberFacilityScope).toHaveBeenCalledWith(
        'member-analyst',
        ['facility-1'],
        2,
      );
    });
    expect(await screen.findByText('1 个机构')).toBeInTheDocument();
  });

  it('shows an issued invitation once without browser storage persistence', async () => {
    const user = userEvent.setup();
    vi.mocked(issueAdminInvitation).mockResolvedValue({
      membership_id: 'member-new',
      token: 'one-time-secret-token',
      expires_at: '2099-08-26T00:00:00Z',
    });

    render(<App />);
    expect(await screen.findByText('邀请成员')).toBeInTheDocument();
    await waitFor(() => expect(listAdminPermissions).toHaveBeenCalledTimes(1));
    await user.type(screen.getByRole('textbox', { name: '邮箱' }), 'new@example.com');
    expect(listAdminPermissions).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('button', { name: '创建邀请' }));

    expect(await screen.findByText('one-time-secret-token')).toBeInTheDocument();
    expect(issueAdminInvitation).toHaveBeenCalledWith('new@example.com', 24);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('suspends a member through the versioned endpoint', async () => {
    const user = userEvent.setup();
    vi.mocked(updateAdminMemberStatus).mockResolvedValue({
      ...analystMember,
      membership_status: 'suspended',
      authorization_version: 2,
      version: 2,
    });

    render(<App />);
    expect(await screen.findByText('Analyst User')).toBeInTheDocument();
    const memberRow = screen.getByText('Analyst User').closest('tr');
    await user.click(within(memberRow as HTMLTableRowElement).getByRole('button', { name: '停用' }));
    await user.click(screen.getByRole('button', { name: '确认' }));

    await waitFor(() => {
      expect(updateAdminMemberStatus).toHaveBeenCalledWith('member-analyst', 'suspended', 1);
    });
    expect(await screen.findByText('已停用')).toBeInTheDocument();
    expect(screen.queryByText('配置 Analyst User')).not.toBeInTheDocument();
  });

  it('lets an import operator sync facilities without reading members', async () => {
    const user = userEvent.setup();
    vi.mocked(getCurrentSession).mockResolvedValue({
      ...session,
      permissions: ['imports.create'],
    });
    const { syncAdminFacilities } = await import('../api');
    vi.mocked(syncAdminFacilities).mockResolvedValue({ created_count: 1, existing_count: 2 });

    render(<App />);
    await user.click(await screen.findByRole('button', { name: '同步机构目录' }));

    await waitFor(() => {
      expect(syncAdminFacilities).toHaveBeenCalledTimes(1);
    });
    expect(listAdminMembers).not.toHaveBeenCalled();
    expect(await screen.findByText(/新增 1 个，已有 2 个/)).toBeInTheDocument();
  });

  it('refreshes the trusted session after changing the current account access', async () => {
    const user = userEvent.setup();
    const currentMember: AdminMember = {
      ...analystMember,
      membership_id: 'member-admin',
      user_id: 'user-admin',
      email: 'admin@example.com',
      display_name: 'Admin User',
      roles: [administratorRole],
    };
    const refreshedSession: AuthSession = {
      ...session,
      permissions: [...session.permissions, 'analytics.schema.read'],
    };
    vi.mocked(getCurrentSession)
      .mockResolvedValueOnce(session)
      .mockResolvedValueOnce(refreshedSession);
    vi.mocked(listAdminMembers).mockResolvedValue({
      items: [currentMember],
      next_cursor: null,
    });
    vi.mocked(listAdminRoles).mockResolvedValue({
      items: [administratorRole, analystRole],
      next_cursor: null,
    });
    vi.mocked(replaceAdminMemberRoles).mockResolvedValue({
      ...currentMember,
      roles: [administratorRole, analystRole],
      authorization_version: 2,
      version: 2,
    });

    render(<App />);
    const memberRow = (await screen.findByText('Admin User')).closest('tr');
    await user.click(
      within(memberRow as HTMLTableRowElement).getByRole('button', { name: '配置权限' }),
    );
    await user.click(screen.getByRole('checkbox', { name: 'Data Analyst' }));
    await user.click(screen.getByRole('button', { name: '保存权限' }));

    await waitFor(() => {
      expect(getCurrentSession).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(await screen.findByText('组织管理')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '工作台' })).toBeInTheDocument();
  });

  it('reports a partial role/scope update instead of presenting it as atomic', async () => {
    const user = userEvent.setup();
    vi.mocked(replaceAdminMemberRoles).mockResolvedValue({
      ...analystMember,
      roles: [analystRole],
      authorization_version: 2,
      version: 2,
    });
    vi.mocked(replaceAdminMemberFacilityScope).mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'ADMIN_VERSION_CONFLICT',
        message: '机构范围已被其他请求修改',
      }),
    );

    render(<App />);
    const memberRow = (await screen.findByText('Analyst User')).closest('tr');
    await user.click(
      within(memberRow as HTMLTableRowElement).getByRole('button', { name: '配置权限' }),
    );
    await user.click(screen.getByRole('checkbox', { name: 'Data Analyst' }));
    await user.click(screen.getByRole('checkbox', { name: /Example Medical Center/ }));
    await user.click(screen.getByRole('button', { name: '保存权限' }));

    expect(await screen.findByText(/部分权限变更可能已提交/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('refreshes the current session while preserving a self-update partial failure', async () => {
    const user = userEvent.setup();
    const currentMember: AdminMember = {
      ...analystMember,
      membership_id: 'member-admin',
      user_id: 'user-admin',
      email: 'admin@example.com',
      display_name: 'Admin User',
      roles: [administratorRole],
    };
    const withRole: AdminMember = {
      ...currentMember,
      roles: [administratorRole, analystRole],
      authorization_version: 2,
      version: 2,
    };
    const refreshedSession: AuthSession = {
      ...session,
      permissions: [...session.permissions, 'analytics.schema.read'],
    };
    vi.mocked(getCurrentSession)
      .mockResolvedValueOnce(session)
      .mockResolvedValueOnce(refreshedSession);
    vi.mocked(listAdminMembers).mockResolvedValue({ items: [currentMember], next_cursor: null });
    vi.mocked(listAdminRoles).mockResolvedValue({
      items: [administratorRole, analystRole],
      next_cursor: null,
    });
    vi.mocked(replaceAdminMemberRoles).mockResolvedValue(withRole);
    vi.mocked(replaceAdminMemberFacilityScope).mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'ADMIN_VERSION_CONFLICT',
        message: '机构范围已被其他请求修改',
      }),
    );

    render(<App />);
    const memberRow = (await screen.findByText('Admin User')).closest('tr');
    await user.click(
      within(memberRow as HTMLTableRowElement).getByRole('button', { name: '配置权限' }),
    );
    await user.click(screen.getByRole('checkbox', { name: 'Data Analyst' }));
    await user.click(screen.getByRole('checkbox', { name: /Example Medical Center/ }));
    await user.click(screen.getByRole('button', { name: '保存权限' }));

    expect(await screen.findByText(/部分权限变更可能已提交/)).toBeInTheDocument();
    await waitFor(() => {
      expect(getCurrentSession).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: '工作台' })).toBeInTheDocument();
    expect(screen.getByText('组织管理')).toBeInTheDocument();
  });
});
