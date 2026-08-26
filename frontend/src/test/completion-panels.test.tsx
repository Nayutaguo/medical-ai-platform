import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuditPanel } from '../AuditPanel';
import { HistoryPanel } from '../HistoryPanel';
import { RoleManagementPanel } from '../RoleManagementPanel';
import {
  createAdminRole,
  listAdminPermissions,
  listAnalysisHistory,
  listAuditEvents,
  updateHistoryFavorite,
} from '../api';
import type { AdminRole, AnalysisHistoryItem } from '../types';

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    createAdminRole: vi.fn(),
    deleteAdminRole: vi.fn(),
    listAdminPermissions: vi.fn(),
    listAdminRoles: vi.fn(),
    listAnalysisHistory: vi.fn(),
    listAuditEvents: vi.fn(),
    updateAdminRole: vi.fn(),
    updateHistoryFavorite: vi.fn(),
    deleteAnalysisHistory: vi.fn(),
  };
});

const historyItem: AnalysisHistoryItem = {
  id: '7',
  kind: 'agent',
  title: '费用分析',
  question: '比较平均费用',
  query_spec: { table: 'inpatient', metrics: [{ field: '*', agg: 'count', alias: 'count' }] },
  chart_spec: null,
  row_count: 2,
  truncated: false,
  query_time_ms: 12,
  is_favorite: false,
  created_at: '2026-08-26T00:00:00Z',
  updated_at: '2026-08-26T00:00:00Z',
  version: 1,
};

describe('completion panels', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lists, reuses and favorites analysis history', async () => {
    const user = userEvent.setup();
    const onReuse = vi.fn();
    vi.mocked(listAnalysisHistory).mockResolvedValue({ items: [historyItem], next_cursor: null });
    vi.mocked(updateHistoryFavorite).mockResolvedValue({ ...historyItem, is_favorite: true, version: 2 });

    render(<HistoryPanel onReuse={onReuse} onAuthenticationLost={vi.fn()} />);
    expect(await screen.findByText('费用分析')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '载入 QuerySpec' }));
    await user.click(screen.getByRole('button', { name: '收藏' }));

    expect(onReuse).toHaveBeenCalledWith(historyItem);
    await waitFor(() => expect(updateHistoryFavorite).toHaveBeenCalledWith('7', true, 1));
    expect(await screen.findByText('已收藏')).toBeInTheDocument();
  });

  it('renders the sanitized audit ledger', async () => {
    vi.mocked(listAuditEvents).mockResolvedValue({
      items: [{
        id: 1,
        occurred_at: '2026-08-26T00:00:00Z',
        request_id: 'request-1',
        actor_kind: 'user',
        actor_user_id: 'user-1',
        action: 'identity.role.create',
        resource_type: 'role',
        resource_id: 'role-1',
        outcome: 'success',
        error_code: null,
        details: { permission_count: 2 },
      }],
      next_cursor: null,
    });

    render(<AuditPanel onAuthenticationLost={vi.fn()} />);
    expect(await screen.findByText('identity.role.create')).toBeInTheDocument();
    expect(screen.getByText(/permission_count/)).toBeInTheDocument();
  });

  it('creates a custom role from the permission catalog', async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const role: AdminRole = {
      id: 'role-viewer', role_key: 'viewer', name: 'Viewer', description: '', permissions: [], is_system: false, version: 1,
    };
    vi.mocked(listAdminPermissions).mockResolvedValue({
      items: [{ id: 'permission-1', permission_key: 'audit.read', resource: 'audit', action: 'read', description: 'Read audit', version: 1 }],
      next_cursor: null,
    });
    vi.mocked(createAdminRole).mockResolvedValue(role);

    render(<RoleManagementPanel roles={[]} onChanged={onChanged} onAuthenticationLost={vi.fn()} />);
    await user.click(await screen.findByRole('button', { name: '新建角色' }));
    await user.type(screen.getByRole('textbox', { name: '角色标识' }), 'viewer');
    await user.type(screen.getByRole('textbox', { name: '角色名称' }), 'Viewer');
    await user.click(await screen.findByRole('checkbox', { name: /audit.read/ }));
    await user.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(createAdminRole).toHaveBeenCalledWith(expect.objectContaining({
      role_key: 'viewer', permission_ids: ['permission-1'],
    })));
    expect(onChanged).toHaveBeenCalled();
  });
});
