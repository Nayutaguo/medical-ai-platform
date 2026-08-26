import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import {
  ApiError,
  getCurrentSession,
  registerAccount,
  requestPasswordReset,
  resetPassword,
} from '../api';

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    getCurrentSession: vi.fn(),
    registerAccount: vi.fn(),
    requestPasswordReset: vi.fn(),
    resetPassword: vi.fn(),
    login: vi.fn(),
  };
});

describe('invitation registration workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCurrentSession).mockRejectedValue(
      new ApiError({
        status: 401,
        code: 'AUTHENTICATION_REQUIRED',
        message: '请先登录',
      }),
    );
  });

  it('prefills the token-bound organization after accepting an invitation', async () => {
    const user = userEvent.setup();
    vi.mocked(registerAccount).mockResolvedValue({
      registered: true,
      organization_id: 'organization-2',
    });

    render(<App />);
    await user.click(await screen.findByRole('tab', { name: '注册' }));
    await user.type(screen.getByRole('textbox', { name: '邀请码' }), 'one-time-token');
    await user.type(screen.getByRole('textbox', { name: '邮箱' }), 'user@example.com');
    await user.type(screen.getByRole('textbox', { name: '显示名称' }), 'User Name');
    await user.type(screen.getByLabelText(/^密码/), 'correct horse battery staple');
    await user.type(screen.getByLabelText(/^确认密码/), 'correct horse battery staple');
    await user.click(screen.getByRole('button', { name: '注册账号' }));

    await waitFor(() => {
      expect(registerAccount).toHaveBeenCalledWith({
        invitation_token: 'one-time-token',
        email: 'user@example.com',
        display_name: 'User Name',
        password: 'correct horse battery staple',
      });
    });
    expect(await screen.findByRole('textbox', { name: '组织 ID（可选）' })).toHaveValue(
      'organization-2',
    );
    expect(screen.getByText(/组织 ID 已自动填写/)).toBeInTheDocument();
  });

  it('completes the loopback password-reset flow without browser storage', async () => {
    const user = userEvent.setup();
    vi.mocked(requestPasswordReset).mockResolvedValue({
      accepted: true,
      reset_token: 'reset-once-token',
      organization_id: 'organization-2',
      expires_at: '2026-08-26T01:00:00Z',
    });
    vi.mocked(resetPassword).mockResolvedValue({ reset: true });

    render(<App />);
    await user.click(await screen.findByRole('button', { name: '忘记密码？' }));
    await user.type(screen.getByRole('textbox', { name: '邮箱' }), 'user@example.com');
    await user.click(screen.getByRole('button', { name: '申请重置' }));

    expect(await screen.findByLabelText(/^重置令牌/)).toHaveValue('reset-once-token');
    expect(screen.getByRole('textbox', { name: '组织 ID' })).toHaveValue('organization-2');
    await user.type(screen.getByLabelText(/^新密码/), 'new password value!');
    await user.type(screen.getByLabelText(/^确认密码/), 'new password value!');
    await user.click(screen.getByRole('button', { name: '重置密码' }));

    await waitFor(() => {
      expect(resetPassword).toHaveBeenCalledWith(
        'reset-once-token',
        'user@example.com',
        'organization-2',
        'new password value!',
      );
    });
    expect(await screen.findByText('密码已重置，请使用新密码登录。')).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('allows production users to paste a reset token delivered out of band', async () => {
    const user = userEvent.setup();
    vi.mocked(requestPasswordReset).mockResolvedValue({ accepted: true });

    render(<App />);
    await user.click(await screen.findByRole('button', { name: '忘记密码？' }));
    await user.type(screen.getByRole('textbox', { name: '邮箱' }), 'user@example.com');
    await user.click(screen.getByRole('button', { name: '申请重置' }));

    expect(await screen.findByText(/如果账号信息匹配/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '我已有重置令牌' }));
    expect(screen.getByLabelText(/^重置令牌/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '组织 ID' })).toBeInTheDocument();
  });
});
