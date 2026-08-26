import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import FactCheckIcon from '@mui/icons-material/FactCheck';

import { isAuthenticationLost, listAuditEvents } from './api';
import type { AuditEvent } from './types';

export function AuditPanel({ onAuthenticationLost }: { onAuthenticationLost: () => void }) {
  const [items, setItems] = useState<AuditEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [action, setAction] = useState('');
  const [outcome, setOutcome] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (append = false) => {
    setLoading(true);
    setError(null);
    try {
      const page = await listAuditEvents(append ? nextCursor ?? undefined : undefined, { action, outcome });
      setItems((current) => (append ? [...current, ...page.items] : page.items));
      setNextCursor(page.next_cursor);
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [action, nextCursor, onAuthenticationLost, outcome]);

  useEffect(() => {
    void load(false);
    // Initial organization-bound page only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Box component="main" sx={{ flex: 1, p: 2, overflow: 'auto' }}>
      <Paper sx={{ p: 2, mx: 'auto', maxWidth: 1320 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} justifyContent="space-between" spacing={2}>
          <Stack direction="row" spacing={1} alignItems="center">
            <FactCheckIcon color="primary" />
            <Box>
              <Typography variant="h2">审计日志</Typography>
              <Typography variant="caption" color="text.secondary">
                当前组织的脱敏操作记录；令牌、密码和医疗结果不会显示。
              </Typography>
            </Box>
          </Stack>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField
              size="small"
              label="操作代码"
              value={action}
              onChange={(event) => setAction(event.target.value)}
              placeholder="例如 identity.role.create"
            />
            <TextField
              size="small"
              select
              label="结果"
              value={outcome}
              onChange={(event) => setOutcome(event.target.value)}
              sx={{ minWidth: 120 }}
            >
              <MenuItem value="">全部</MenuItem>
              <MenuItem value="success">成功</MenuItem>
              <MenuItem value="denied">拒绝</MenuItem>
              <MenuItem value="failure">失败</MenuItem>
            </TextField>
            <Button variant="outlined" onClick={() => void load(false)} disabled={loading}>查询</Button>
          </Stack>
        </Stack>
        {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
        <TableContainer sx={{ mt: 2, maxHeight: 'calc(100vh - 230px)' }}>
          <Table stickyHeader size="small">
            <TableHead>
              <TableRow>
                <TableCell>时间</TableCell>
                <TableCell>操作</TableCell>
                <TableCell>操作者</TableCell>
                <TableCell>资源</TableCell>
                <TableCell>结果</TableCell>
                <TableCell>错误代码</TableCell>
                <TableCell>request_id</TableCell>
                <TableCell>详情</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((item) => (
                <TableRow key={item.id} hover>
                  <TableCell sx={{ whiteSpace: 'nowrap' }}>{formatDateTime(item.occurred_at)}</TableCell>
                  <TableCell>{item.action}</TableCell>
                  <TableCell>{item.actor_kind === 'system' ? 'system' : item.actor_user_id ?? 'user'}</TableCell>
                  <TableCell>{item.resource_type}{item.resource_id ? ` / ${item.resource_id}` : ''}</TableCell>
                  <TableCell>{outcomeLabel(item.outcome)}</TableCell>
                  <TableCell>{item.error_code ?? '—'}</TableCell>
                  <TableCell sx={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.request_id ?? '—'}</TableCell>
                  <TableCell>
                    <Box component="pre" sx={{ m: 0, maxWidth: 360, whiteSpace: 'pre-wrap', fontSize: 11 }}>
                      {Object.keys(item.details).length ? JSON.stringify(item.details, null, 2) : '—'}
                    </Box>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
        {!loading && items.length === 0 ? <Alert severity="info" sx={{ mt: 2 }}>没有符合条件的审计记录。</Alert> : null}
        <Stack alignItems="center" sx={{ mt: 2 }}>
          {loading ? <CircularProgress size={24} /> : null}
          {nextCursor && !loading ? <Button onClick={() => void load(true)}>加载更多</Button> : null}
        </Stack>
      </Paper>
    </Box>
  );
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function outcomeLabel(value: AuditEvent['outcome']): string {
  return { success: '成功', denied: '拒绝', failure: '失败' }[value];
}
