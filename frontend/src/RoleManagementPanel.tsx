import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  Paper,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import EditIcon from '@mui/icons-material/Edit';

import {
  createAdminRole,
  deleteAdminRole,
  isAuthenticationLost,
  listAdminPermissions,
  updateAdminRole,
} from './api';
import type { AdminPermission, AdminRole } from './types';

export function RoleManagementPanel({
  roles,
  onChanged,
  onAuthenticationLost,
}: {
  roles: AdminRole[];
  onChanged: () => Promise<void>;
  onAuthenticationLost: () => void;
}) {
  const [permissions, setPermissions] = useState<AdminPermission[]>([]);
  const [permissionsLoaded, setPermissionsLoaded] = useState(false);
  const [editing, setEditing] = useState<AdminRole | 'new' | null>(null);
  const [roleKey, setRoleKey] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [permissionIds, setPermissionIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const permissionByKey = useMemo(
    () => new Map(permissions.map((permission) => [permission.permission_key, permission])),
    [permissions],
  );

  useEffect(() => {
    setPermissionsLoaded(false);
    collectPages(listAdminPermissions)
      .then((items) => {
        setPermissions(items);
        setPermissionsLoaded(true);
      })
      .catch((cause) => {
        if (isAuthenticationLost(cause)) onAuthenticationLost();
        setError(cause instanceof Error ? cause.message : String(cause));
      });
  }, [onAuthenticationLost]);

  const openEditor = (role: AdminRole | 'new') => {
    setError(null);
    if (!permissionsLoaded) {
      setError('权限目录尚未加载，暂时不能编辑角色。');
      return;
    }
    setEditing(role);
    if (role === 'new') {
      setRoleKey('');
      setName('');
      setDescription('');
      setPermissionIds([]);
    } else {
      const resolvedPermissionIds = role.permissions
        .map((key) => permissionByKey.get(key)?.id)
        .filter((id): id is string => Boolean(id));
      if (resolvedPermissionIds.length !== role.permissions.length) {
        setEditing(null);
        setError('角色包含当前目录无法识别的权限，请刷新后重试。');
        return;
      }
      setRoleKey(role.role_key);
      setName(role.name);
      setDescription(role.description ?? '');
      setPermissionIds(resolvedPermissionIds);
    }
  };

  const save = async () => {
    if (!editing || !permissionsLoaded || !roleKey.trim() || !name.trim()) return;
    setSaving(true);
    setError(null);
    try {
      if (editing === 'new') {
        await createAdminRole({
          role_key: roleKey.trim(),
          name: name.trim(),
          description: description.trim(),
          permission_ids: permissionIds,
        });
      } else {
        await updateAdminRole(editing.id, {
          name: name.trim(),
          description: description.trim(),
          permission_ids: permissionIds,
          expected_version: editing.version,
        });
      }
      setEditing(null);
      await onChanged();
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (role: AdminRole) => {
    if (!window.confirm(`删除自定义角色“${role.name}”？`)) return;
    setSaving(true);
    setError(null);
    try {
      await deleteAdminRole(role.id, role.version);
      await onChanged();
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Paper variant="outlined" sx={{ p: 1.5 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1}>
        <Box>
          <Typography variant="h2">角色与权限</Typography>
          <Typography variant="caption" color="text.secondary">内置角色只读；自定义角色可按权限粒度配置。</Typography>
        </Box>
        <Button
          startIcon={<AddIcon />}
          variant="outlined"
          onClick={() => openEditor('new')}
          disabled={!permissionsLoaded}
        >
          {permissionsLoaded ? '新建角色' : '加载权限中…'}
        </Button>
      </Stack>
      {error ? <Alert severity="error" sx={{ mt: 1.5 }}>{error}</Alert> : null}
      <Stack spacing={1} sx={{ mt: 1.5 }}>
        {roles.map((role) => (
          <Paper key={role.id} variant="outlined" sx={{ p: 1.25 }}>
            <Stack direction="row" justifyContent="space-between" spacing={1}>
              <Box minWidth={0}>
                <Typography variant="body2" fontWeight={700}>
                  {role.name} {role.is_system ? '（内置）' : ''}
                </Typography>
                <Typography variant="caption" color="text.secondary">{role.role_key} · {role.permissions.join('、') || '无权限'}</Typography>
              </Box>
              {!role.is_system ? (
                <Stack direction="row">
                  <Tooltip title="编辑"><IconButton size="small" onClick={() => openEditor(role)}><EditIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="删除"><IconButton size="small" color="error" onClick={() => void remove(role)}><DeleteOutlineIcon fontSize="small" /></IconButton></Tooltip>
                </Stack>
              ) : null}
            </Stack>
          </Paper>
        ))}
      </Stack>

      <Dialog open={editing !== null} onClose={() => !saving && setEditing(null)} fullWidth maxWidth="sm">
        <DialogTitle>{editing === 'new' ? '新建自定义角色' : '编辑自定义角色'}</DialogTitle>
        <DialogContent>
          <Stack spacing={1.5} sx={{ mt: 1 }}>
            <TextField
              label="角色标识"
              value={roleKey}
              onChange={(event) => setRoleKey(event.target.value)}
              disabled={editing !== 'new'}
              required
              helperText="小写字母、数字和下划线，例如 report_viewer。创建后不可修改。"
            />
            <TextField label="角色名称" value={name} onChange={(event) => setName(event.target.value)} required />
            <TextField label="说明" value={description} onChange={(event) => setDescription(event.target.value)} multiline minRows={2} />
            <Typography variant="body2" fontWeight={700}>权限</Typography>
            <Box sx={{ maxHeight: 300, overflow: 'auto', border: '1px solid rgba(16,24,40,0.12)', borderRadius: 1, p: 1 }}>
              {permissions.map((permission) => (
                <FormControlLabel
                  key={permission.id}
                  control={
                    <Checkbox
                      checked={permissionIds.includes(permission.id)}
                      onChange={() => setPermissionIds((current) => toggle(current, permission.id))}
                    />
                  }
                  label={`${permission.permission_key}${permission.description ? ` — ${permission.description}` : ''}`}
                  sx={{ display: 'flex', alignItems: 'flex-start', m: 0 }}
                />
              ))}
            </Box>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditing(null)} disabled={saving}>取消</Button>
          <Button
            variant="contained"
            onClick={() => void save()}
            disabled={saving || !permissionsLoaded || !roleKey.trim() || !name.trim()}
          >
            保存
          </Button>
        </DialogActions>
      </Dialog>
    </Paper>
  );
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

async function collectPages<T>(loader: (cursor?: string) => Promise<{ items: T[]; next_cursor: string | null }>): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (let pageNumber = 0; pageNumber < 20; pageNumber += 1) {
    const page = await loader(cursor);
    items.push(...page.items);
    if (!page.next_cursor) return items;
    cursor = page.next_cursor;
  }
  throw new Error('角色权限数据超过当前页面限制');
}
