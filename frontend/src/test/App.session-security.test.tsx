import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import {
  ApiError,
  ask,
  changePassword,
  getCurrentSession,
  getHealth,
  getSchema,
  login,
  logout,
} from '../api';
import type { AskPayload, AuthSession } from '../types';

vi.mock('echarts', () => ({
  init: vi.fn(() => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  })),
  getInstanceByDom: vi.fn(() => null),
}));

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    ask: vi.fn(),
    changePassword: vi.fn(),
    getCurrentSession: vi.fn(),
    getHealth: vi.fn(),
    getSchema: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  };
});

const allAnalyticsPermissions = [
  'analytics.schema.read',
  'analytics.distinct.read',
  'analytics.query.execute',
  'analytics.agent.execute',
];

const sessionA = session('user-a', 'member-a', allAnalyticsPermissions);
const sessionB = session('user-b', 'member-b', allAnalyticsPermissions);

describe('session-bound workspace state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCurrentSession).mockResolvedValue(sessionA);
    vi.mocked(getHealth).mockResolvedValue({ status: 'alive' });
    vi.mocked(getSchema).mockResolvedValue({ tables: [{ name: 'inpatient', description: '', columns: [] }] });
    vi.mocked(logout).mockResolvedValue(undefined);
    vi.mocked(login).mockResolvedValue(sessionB);
    vi.mocked(changePassword).mockResolvedValue(undefined);
  });

  it('drops a late Agent response after logout and another user login', async () => {
    const user = userEvent.setup();
    let resolveAgent!: (value: AskPayload) => void;
    vi.mocked(ask).mockImplementationOnce(() => new Promise((resolve) => {
      resolveAgent = resolve;
    }));

    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Run Agent' }));
    await user.click(screen.getByRole('button', { name: '退出' }));
    await user.type(await screen.findByRole('textbox', { name: '邮箱' }), 'b@example.com');
    await user.type(screen.getByLabelText(/^密码/), 'user b password');
    await user.click(screen.getByRole('button', { name: '登录' }));
    expect(await screen.findByRole('button', { name: 'Run Agent' })).toBeInTheDocument();

    await act(async () => resolveAgent(agentResult('private aggregate from user A')));

    await waitFor(() => expect(screen.queryByText('private aggregate from user A')).not.toBeInTheDocument());
  });

  it('clears prior results when password change forces reauthentication', async () => {
    const user = userEvent.setup();
    vi.mocked(ask).mockResolvedValue(agentResult('private aggregate from user A'));

    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Run Agent' }));
    expect(await screen.findByText('private aggregate from user A')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '修改密码' }));
    await user.type(screen.getByLabelText(/^当前密码/), 'old password value');
    await user.type(screen.getByLabelText(/^新密码/), 'new password value!');
    await user.type(screen.getByLabelText(/^确认新密码/), 'new password value!');
    await user.click(screen.getByRole('button', { name: '确认修改' }));

    await user.type(await screen.findByRole('textbox', { name: '邮箱' }), 'b@example.com');
    await user.type(screen.getByLabelText(/^密码/), 'user b password');
    await user.click(screen.getByRole('button', { name: '登录' }));
    expect(await screen.findByRole('button', { name: 'Run Agent' })).toBeInTheDocument();
    expect(screen.queryByText('private aggregate from user A')).not.toBeInTheDocument();
  });

  it('fails closed when password change discovers an expired session', async () => {
    const user = userEvent.setup();
    vi.mocked(ask).mockResolvedValue(agentResult('private aggregate from expired user'));
    vi.mocked(changePassword).mockRejectedValue(new ApiError({
      status: 401,
      code: 'AUTHENTICATION_REQUIRED',
      message: 'session expired',
    }));

    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Run Agent' }));
    expect(await screen.findByText('private aggregate from expired user')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '修改密码' }));
    await user.type(screen.getByLabelText(/^当前密码/), 'old password value');
    await user.type(screen.getByLabelText(/^新密码/), 'new password value!');
    await user.type(screen.getByLabelText(/^确认新密码/), 'new password value!');
    await user.click(screen.getByRole('button', { name: '确认修改' }));

    expect(await screen.findByRole('button', { name: '登录' })).toBeInTheDocument();
    expect(screen.queryByText('private aggregate from expired user')).not.toBeInTheDocument();
  });

  it('honors exact custom-role permissions in the workbench UI', async () => {
    vi.mocked(getCurrentSession).mockResolvedValue(
      session('query-user', 'query-member', ['analytics.query.execute']),
    );

    render(<App />);

    expect(await screen.findByRole('button', { name: 'Run QuerySpec' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Run Agent' })).toBeDisabled();
    expect(getSchema).not.toHaveBeenCalled();
  });
});

function session(userId: string, membershipId: string, permissions: string[]): AuthSession {
  return {
    user: { id: userId, email: `${userId}@example.com`, display_name: userId },
    organization: { id: 'organization-1', name: 'Hospital', membership_id: membershipId },
    permissions,
    expires_at: '2099-08-26T00:00:00Z',
    idle_expires_at: '2099-08-25T23:00:00Z',
  };
}

function agentResult(label: string): AskPayload {
  return {
    question: 'safe aggregate question',
    query_spec: null,
    compiled_sql: null,
    compiled_params: {},
    chart_spec: { chart_type: 'table', title: 'Result', reason: 'test' },
    result: {
      columns: ['label', 'value'],
      rows: [{ label, value: 1 }],
      row_count: 1,
      query_time_ms: 1,
      truncated: false,
      metadata: {},
    },
  };
}
