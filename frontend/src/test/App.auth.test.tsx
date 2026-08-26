import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import { ApiError, getCurrentSession, registerAccount } from '../api';

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    getCurrentSession: vi.fn(),
    registerAccount: vi.fn(),
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
});
