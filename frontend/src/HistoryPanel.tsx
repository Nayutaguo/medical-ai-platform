import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  IconButton,
  Paper,
  Stack,
  Switch,
  Tooltip,
  Typography,
} from '@mui/material';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import HistoryIcon from '@mui/icons-material/History';
import ReplayIcon from '@mui/icons-material/Replay';
import StarIcon from '@mui/icons-material/Star';
import StarBorderIcon from '@mui/icons-material/StarBorder';

import {
  deleteAnalysisHistory,
  isAuthenticationLost,
  listAnalysisHistory,
  updateHistoryFavorite,
} from './api';
import type { AnalysisHistoryItem } from './types';

export function HistoryPanel({
  onReuse,
  onAuthenticationLost,
}: {
  onReuse: (item: AnalysisHistoryItem) => void;
  onAuthenticationLost: () => void;
}) {
  const [items, setItems] = useState<AnalysisHistoryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (append = false) => {
    setLoading(true);
    setError(null);
    try {
      const page = await listAnalysisHistory(append ? nextCursor ?? undefined : undefined, favoriteOnly);
      setItems((current) => (append ? [...current, ...page.items] : page.items));
      setNextCursor(page.next_cursor);
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [favoriteOnly, nextCursor, onAuthenticationLost]);

  useEffect(() => {
    void load(false);
    // nextCursor is intentionally excluded: it is an output of the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [favoriteOnly]);

  const toggleFavorite = async (item: AnalysisHistoryItem) => {
    try {
      const updated = await updateHistoryFavorite(item.id, !item.is_favorite, item.version);
      setItems((current) =>
        favoriteOnly && !updated.is_favorite
          ? current.filter((candidate) => candidate.id !== updated.id)
          : current.map((candidate) => (candidate.id === updated.id ? updated : candidate)),
      );
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const remove = async (item: AnalysisHistoryItem) => {
    if (!window.confirm(`删除历史记录“${item.title}”？`)) return;
    try {
      await deleteAnalysisHistory(item.id);
      setItems((current) => current.filter((candidate) => candidate.id !== item.id));
    } catch (cause) {
      if (isAuthenticationLost(cause)) onAuthenticationLost();
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <Box component="main" sx={{ flex: 1, p: 2, overflow: 'auto' }}>
      <Paper sx={{ maxWidth: 1100, mx: 'auto', p: 2 }}>
        <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" spacing={1.5}>
          <Stack direction="row" spacing={1} alignItems="center">
            <HistoryIcon color="primary" />
            <Box>
              <Typography variant="h2">查询历史与收藏</Typography>
              <Typography variant="caption" color="text.secondary">
                仅保存受控 QuerySpec、图表结构和统计摘要，不保存原始问题或结果明细；重放时使用最新数据与权限。
              </Typography>
            </Box>
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center">
            <Typography variant="body2">只看收藏</Typography>
            <Switch
              checked={favoriteOnly}
              onChange={(event) => setFavoriteOnly(event.target.checked)}
              inputProps={{ 'aria-label': '只看收藏' }}
            />
            <Button onClick={() => void load(false)} disabled={loading}>刷新</Button>
          </Stack>
        </Stack>
        {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
        <Stack spacing={1.25} sx={{ mt: 2 }}>
          {!loading && items.length === 0 ? (
            <Alert severity="info">还没有历史记录。完成一次自然语言分析或 QuerySpec 查询后会自动出现。</Alert>
          ) : null}
          {items.map((item) => (
            <Paper key={item.id} variant="outlined" sx={{ p: 1.5 }}>
              <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" spacing={1}>
                <Box minWidth={0}>
                  <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap">
                    <Chip size="small" label={item.kind === 'agent' ? 'AI 分析' : '结构化查询'} color={item.kind === 'agent' ? 'primary' : 'default'} />
                    {item.is_favorite ? <Chip size="small" label="已收藏" color="warning" /> : null}
                    <Typography variant="body1" fontWeight={700}>{item.title}</Typography>
                  </Stack>
                  {item.question ? (
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>{item.question}</Typography>
                  ) : null}
                  <Typography variant="caption" color="text.secondary">
                    {formatDateTime(item.created_at)} · {item.row_count} 行{item.truncated ? '（已截断）' : ''} · {item.query_time_ms} ms
                  </Typography>
                </Box>
                <Stack direction="row" alignItems="center" alignSelf={{ xs: 'flex-end', sm: 'center' }}>
                  <Tooltip title={item.is_favorite ? '取消收藏' : '收藏'}>
                    <IconButton onClick={() => void toggleFavorite(item)}>
                      {item.is_favorite ? <StarIcon color="warning" /> : <StarBorderIcon />}
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={item.query_spec ? '载入 QuerySpec' : '该记录没有可重放的 QuerySpec'}>
                    <span>
                      <IconButton
                        aria-label={item.query_spec ? '载入 QuerySpec' : '该记录没有可重放的 QuerySpec'}
                        onClick={() => onReuse(item)}
                        disabled={!item.query_spec}
                      >
                        <ReplayIcon />
                      </IconButton>
                    </span>
                  </Tooltip>
                  <Tooltip title="删除">
                    <IconButton color="error" onClick={() => void remove(item)}><DeleteOutlineIcon /></IconButton>
                  </Tooltip>
                </Stack>
              </Stack>
            </Paper>
          ))}
          {loading ? <CircularProgress size={24} sx={{ alignSelf: 'center' }} /> : null}
          {nextCursor ? (
            <Button variant="outlined" onClick={() => void load(true)} disabled={loading}>加载更多</Button>
          ) : null}
        </Stack>
      </Paper>
    </Box>
  );
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
