import { useState } from 'react';
import {
  Alert,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
} from '@mui/material';

import { changePassword, isAuthenticationLost } from './api';

export function PasswordChangeDialog({
  open,
  onClose,
  onChanged,
  onAuthenticationLost,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
  onAuthenticationLost: () => void;
}) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    if (loading) return;
    setCurrentPassword('');
    setNewPassword('');
    setConfirmation('');
    setError(null);
    onClose();
  };

  const submit = async () => {
    setError(null);
    if (newPassword.length < 12) {
      setError('新密码至少需要 12 个字符。');
      return;
    }
    if (newPassword !== confirmation) {
      setError('两次输入的新密码不一致。');
      return;
    }
    setLoading(true);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmation('');
      onChanged();
    } catch (cause) {
      if (isAuthenticationLost(cause)) {
        onAuthenticationLost();
        return;
      }
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} fullWidth maxWidth="xs">
      <DialogTitle>修改密码</DialogTitle>
      <DialogContent>
        <Stack spacing={1.5} sx={{ mt: 1 }}>
          {error ? <Alert severity="error">{error}</Alert> : null}
          <Alert severity="info">修改成功后所有登录会话都会失效，需要使用新密码重新登录。</Alert>
          <TextField label="当前密码" type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" required />
          <TextField label="新密码" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required helperText="至少 12 个字符；最终规则以服务端为准。" />
          <TextField label="确认新密码" type="password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" required error={Boolean(confirmation && confirmation !== newPassword)} />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={close} disabled={loading}>取消</Button>
        <Button variant="contained" onClick={() => void submit()} disabled={loading || !currentPassword || !newPassword || !confirmation}>
          {loading ? <CircularProgress size={18} color="inherit" /> : '确认修改'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
