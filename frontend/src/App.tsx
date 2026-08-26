import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactElement, ReactNode } from 'react';
import * as echarts from 'echarts';
import {
  Alert,
  AppBar,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  CssBaseline,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  IconButton,
  List,
  ListItemButton,
  ListItemText,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tab,
  Tabs,
  TextField,
  ThemeProvider,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import RefreshIcon from '@mui/icons-material/Refresh';
import StorageIcon from '@mui/icons-material/Storage';
import SchemaIcon from '@mui/icons-material/Schema';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import QueryStatsIcon from '@mui/icons-material/QueryStats';
import TableRowsIcon from '@mui/icons-material/TableRows';
import ManageAccountsIcon from '@mui/icons-material/ManageAccounts';
import DashboardIcon from '@mui/icons-material/Dashboard';
import HistoryIcon from '@mui/icons-material/History';
import FactCheckIcon from '@mui/icons-material/FactCheck';
import KeyIcon from '@mui/icons-material/Key';
import DownloadIcon from '@mui/icons-material/Download';
import ImageIcon from '@mui/icons-material/Image';
import DescriptionIcon from '@mui/icons-material/Description';
import {
  ApiError,
  ask,
  getCurrentSession,
  getDistinct,
  getHealth,
  getSchema,
  isAuthenticationLost,
  isAuthenticationDisabled,
  login,
  issueAdminInvitation,
  listAdminFacilities,
  listAdminMembers,
  listAdminRoles,
  logout,
  requestPasswordReset,
  registerAccount,
  replaceAdminMemberFacilityScope,
  replaceAdminMemberRoles,
  runQuerySpec,
  resetPassword,
  syncAdminFacilities,
  updateAdminMemberStatus,
} from './api';
import { AuditPanel } from './AuditPanel';
import { CHART_ELEMENT_ID, exportChartPng, exportHtmlReport, exportResultCsv } from './exports';
import { HistoryPanel } from './HistoryPanel';
import { PasswordChangeDialog } from './PasswordChangeDialog';
import { RoleManagementPanel } from './RoleManagementPanel';
import { theme } from './theme';
import type {
  AgentInsight,
  AgentIntent,
  AnalysisHistoryItem,
  AdminFacility,
  AdminMember,
  AdminRole,
  AskPayload,
  AuthSession,
  ChartSpec,
  DistinctPayload,
  HealthPayload,
  QueryResult,
  RegistrationCredentials,
  SchemaColumn,
  SchemaPayload,
} from './types';

type AuthStatus = 'checking' | 'authenticated' | 'anonymous' | 'unauthenticated' | 'error';
type AuthFieldError = { field: string; message: string };
type AppView = 'workbench' | 'history' | 'administration' | 'audit';

const defaultQuestion = '2021年50到69岁和70岁以上患者的平均总费用是多少，按年龄组排序';
const sampleSpec = {
  table: 'inpatient',
  filters: [
    { field: 'DischargeYear', op: '=', value: 2021 },
    { field: 'AgeGroup', op: 'in', value: ['50to69', '70orOlder'] },
  ],
  group_by: ['AgeGroup'],
  metrics: [
    { field: 'TotalCharges', agg: 'avg', alias: 'avg_total_charges' },
    { field: '*', agg: 'count', alias: 'patient_count' },
  ],
  order_by: [{ field: 'avg_total_charges', direction: 'desc' }],
  limit: 100,
};

const sampleQuestions = [
  '按年龄组统计2021年患者数量分布',
  '2021年不同入院类型的平均住院天数排名前10',
  '比较不同支付方式的平均总费用',
  '2021年急诊和非急诊患者的平均总费用有什么差异',
];

function defaultView(session: AuthSession): AppView {
  if (session.permissions.includes('analytics.schema.read')) return 'workbench';
  if (session.permissions.some((permission) => ['users.manage', 'roles.assign', 'imports.create'].includes(permission))) {
    return 'administration';
  }
  if (session.permissions.includes('audit.read')) return 'audit';
  return 'workbench';
}

export function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus>('checking');
  const [session, setSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authFieldErrors, setAuthFieldErrors] = useState<AuthFieldError[]>([]);
  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [activeView, setActiveView] = useState<AppView>('workbench');
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [schema, setSchema] = useState<SchemaPayload | null>(null);
  const [distinct, setDistinct] = useState<DistinctPayload | null>(null);
  const [question, setQuestion] = useState(defaultQuestion);
  const [querySpecText, setQuerySpecText] = useState(JSON.stringify(sampleSpec, null, 2));
  const [askResult, setAskResult] = useState<AskPayload | null>(null);
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null);
  const [executedQuerySpec, setExecutedQuerySpec] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const authEpochRef = useRef(0);
  const isAnonymous = authStatus === 'anonymous';
  const canReadSchema = isAnonymous || session?.permissions.includes('analytics.schema.read') === true;
  const canReadDistinct = isAnonymous || session?.permissions.includes('analytics.distinct.read') === true;
  const canRunQuery = isAnonymous || session?.permissions.includes('analytics.query.execute') === true;
  const canRunAgent =
    isAnonymous ||
    (session?.permissions.includes('analytics.agent.execute') === true &&
      canReadSchema &&
      canReadDistinct &&
      canRunQuery);
  const canAccessWorkbench =
    isAnonymous ||
    (authStatus === 'authenticated' && (canReadSchema || canReadDistinct || canRunQuery || canRunAgent));
  const canManageUsers = session?.permissions.includes('users.manage') === true;
  const canAssignRoles = session?.permissions.includes('roles.assign') === true;
  const canCreateImports = session?.permissions.includes('imports.create') === true;
  const canReadAudit = session?.permissions.includes('audit.read') === true;
  const canUseHistory =
    authStatus === 'authenticated' &&
    (session?.permissions.includes('analytics.query.execute') === true ||
      session?.permissions.includes('analytics.agent.execute') === true);
  const canAccessAdministration =
    authStatus === 'authenticated' && (canManageUsers || canAssignRoles || canCreateImports);

  const clearProtectedWorkspaceState = useCallback(() => {
    setHealth(null);
    setSchema(null);
    setDistinct(null);
    setAskResult(null);
    setQueryResult(null);
    setExecutedQuerySpec(null);
    setQuestion(defaultQuestion);
    setQuerySpecText(JSON.stringify(sampleSpec, null, 2));
    setLoading(false);
    setError(null);
  }, []);

  const invalidateProtectedWorkspace = useCallback(() => {
    authEpochRef.current += 1;
    clearProtectedWorkspaceState();
  }, [clearProtectedWorkspaceState]);

  const reportRequestFailure = useCallback((err: unknown) => {
    if (isAuthenticationLost(err)) {
      invalidateProtectedWorkspace();
      setSession(null);
      setAuthStatus('unauthenticated');
    }
    setError(err instanceof Error ? err.message : String(err));
  }, [invalidateProtectedWorkspace]);

  const loadBasics = useCallback(async () => {
    const epoch = authEpochRef.current;
    setError(null);
    try {
      const [nextHealth, nextSchema] = await Promise.all([
        getHealth(),
        canReadSchema ? getSchema() : Promise.resolve(null),
      ]);
      if (authEpochRef.current !== epoch) return;
      setHealth(nextHealth);
      setSchema(nextSchema);
    } catch (err) {
      if (authEpochRef.current !== epoch) return;
      reportRequestFailure(err);
    }
  }, [canReadSchema, reportRequestFailure]);

  const restoreAuthentication = useCallback(async () => {
    invalidateProtectedWorkspace();
    const epoch = authEpochRef.current;
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
    setAuthStatus('checking');
    try {
      const activeSession = await getCurrentSession();
      if (authEpochRef.current !== epoch) return;
      setSession(activeSession);
      setActiveView(defaultView(activeSession));
      setAuthStatus('authenticated');
    } catch (err) {
      if (authEpochRef.current !== epoch) return;
      setSession(null);
      if (isAuthenticationDisabled(err)) {
        setAuthStatus('anonymous');
        return;
      }
      if (isAuthenticationLost(err)) {
        setAuthStatus('unauthenticated');
        return;
      }
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
      setAuthStatus('error');
    }
  }, [invalidateProtectedWorkspace]);

  const refreshCurrentSession = useCallback(async () => {
    const epoch = authEpochRef.current;
    const activeSession = await getCurrentSession();
    if (authEpochRef.current === epoch) setSession(activeSession);
  }, []);

  useEffect(() => {
    void restoreAuthentication();
  }, [restoreAuthentication]);

  useEffect(() => {
    if (canAccessWorkbench && activeView === 'workbench') {
      void loadBasics();
    }
  }, [activeView, canAccessWorkbench, loadBasics]);

  const handleLogin = async (email: string, password: string, organizationId: string) => {
    setAuthLoading(true);
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
    setError(null);
    try {
      const activeSession = await login({
        email,
        password,
        ...(organizationId.trim() ? { organization_id: organizationId.trim() } : {}),
      });
      invalidateProtectedWorkspace();
      setSession(activeSession);
      setActiveView(defaultView(activeSession));
      setAuthStatus('authenticated');
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleRegistration = async (credentials: RegistrationCredentials): Promise<string | null> => {
    setAuthLoading(true);
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
    setError(null);
    try {
      const result = await registerAccount(credentials);
      setAuthNotice('邀请已接受，请使用该账号登录。组织 ID 已自动填写，数据权限由组织管理员分配。');
      return result.organization_id;
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const clearAuthFeedback = () => {
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
  };

  const handleLogout = async () => {
    setAuthLoading(true);
    invalidateProtectedWorkspace();
    setActiveView('workbench');
    try {
      await logout();
      setSession(null);
      setAuthStatus('unauthenticated');
    } catch (err) {
      if (isAuthenticationLost(err)) {
        setSession(null);
        setAuthStatus('unauthenticated');
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAuthLoading(false);
    }
  };

  const handleAsk = async () => {
    const epoch = authEpochRef.current;
    setLoading(true);
    setError(null);
    setAskResult(null);
    setQueryResult(null);
    try {
      const payload = await ask(question);
      if (authEpochRef.current !== epoch) return;
      setAskResult(payload);
      setExecutedQuerySpec(payload.query_spec ?? null);
      if (payload.query_spec) {
        setQuerySpecText(JSON.stringify(payload.query_spec, null, 2));
      }
      setQueryResult(payload.result ?? null);
    } catch (err) {
      if (authEpochRef.current !== epoch) return;
      reportRequestFailure(err);
    } finally {
      if (authEpochRef.current === epoch) setLoading(false);
    }
  };

  const handleRunSpec = async () => {
    const epoch = authEpochRef.current;
    setLoading(true);
    setError(null);
    setAskResult(null);
    setQueryResult(null);
    try {
      const parsed = JSON.parse(querySpecText) as Record<string, unknown>;
      const payload = await runQuerySpec(parsed);
      if (authEpochRef.current !== epoch) return;
      setAskResult(null);
      setExecutedQuerySpec(parsed);
      setQueryResult(payload);
    } catch (err) {
      if (authEpochRef.current !== epoch) return;
      reportRequestFailure(err);
    } finally {
      if (authEpochRef.current === epoch) setLoading(false);
    }
  };

  const handleDistinct = async (field: string) => {
    const epoch = authEpochRef.current;
    setError(null);
    try {
      const nextDistinct = await getDistinct(field);
      if (authEpochRef.current !== epoch) return;
      setDistinct(nextDistinct);
    } catch (err) {
      if (authEpochRef.current !== epoch) return;
      reportRequestFailure(err);
    }
  };

  const handleAuthenticationLost = useCallback(() => {
    invalidateProtectedWorkspace();
    setPasswordDialogOpen(false);
    setSession(null);
    setAuthStatus('unauthenticated');
    setActiveView('workbench');
  }, [invalidateProtectedWorkspace]);

  useEffect(() => {
    if (authStatus !== 'authenticated' || !session) return undefined;
    const expiries = [session.expires_at, session.idle_expires_at]
      .map((value) => new Date(value).getTime())
      .filter(Number.isFinite);
    if (expiries.length !== 2) {
      handleAuthenticationLost();
      return undefined;
    }
    const delay = Math.max(0, Math.min(...expiries) - Date.now() + 50);
    const timer = window.setTimeout(async () => {
      const epoch = authEpochRef.current;
      try {
        const refreshed = await getCurrentSession();
        if (authEpochRef.current === epoch) setSession(refreshed);
      } catch {
        if (authEpochRef.current === epoch) handleAuthenticationLost();
      }
    }, Math.min(delay, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [authStatus, handleAuthenticationLost, session]);

  const handlePasswordChanged = () => {
    setPasswordDialogOpen(false);
    invalidateProtectedWorkspace();
    setSession(null);
    setAuthNotice('密码已修改，请使用新密码重新登录。');
    setAuthStatus('unauthenticated');
    setActiveView('workbench');
  };

  const reuseHistory = (item: AnalysisHistoryItem) => {
    if (!item.query_spec) return;
    setQuerySpecText(JSON.stringify(item.query_spec, null, 2));
    if (item.question) setQuestion(item.question);
    setAskResult(null);
    setQueryResult(null);
    setExecutedQuerySpec(null);
    setError(null);
    setActiveView('workbench');
  };

  const exportTitle = askResult?.chart_spec?.title || askResult?.analysis_goal || '医疗数据分析结果';

  const runExport = (operation: () => void) => {
    setError(null);
    try {
      operation();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const columns = schema?.tables[0]?.columns ?? [];

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ minHeight: '100vh', bgcolor: 'background.default', display: 'flex', flexDirection: 'column' }}>
        <AppBar position="static" color="inherit" elevation={0} sx={{ border: 0, borderBottom: '1px solid rgba(16,24,40,0.12)' }}>
          <Toolbar sx={{ minHeight: 68, gap: 2, justifyContent: 'space-between' }}>
            <Box>
              <Typography variant="h1">Medical AI Workbench</Typography>
              <Typography variant="body2" color="text.secondary">
                受治理的住院出院数据分析
              </Typography>
            </Box>
            {authStatus === 'authenticated' || authStatus === 'anonymous' ? (
              <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" justifyContent="flex-end" alignItems="center">
                {canAccessWorkbench ? (
                  <>
                    <StatusChip icon={<StorageIcon />} label={health?.status === 'alive' ? '服务在线' : '服务检查中'} ok={health?.status === 'alive'} />
                    {canReadSchema ? <Chip size="small" label={schema ? `${columns.length} fields` : 'Schema --'} /> : null}
                  </>
                ) : null}
                {session ? (
                  <>
                    <Chip
                      size="small"
                      variant="outlined"
                      label={`${session.user.display_name || session.user.email} · ${session.organization.name}`}
                    />
                    {canAccessWorkbench ? (
                      <Button
                        size="small"
                        color="inherit"
                        startIcon={<DashboardIcon />}
                        variant={activeView === 'workbench' ? 'outlined' : 'text'}
                        onClick={() => setActiveView('workbench')}
                      >
                        工作台
                      </Button>
                    ) : null}
                    {canUseHistory ? (
                      <Button
                        size="small"
                        color="inherit"
                        startIcon={<HistoryIcon />}
                        variant={activeView === 'history' ? 'outlined' : 'text'}
                        onClick={() => setActiveView('history')}
                      >
                        历史
                      </Button>
                    ) : null}
                    {canAccessAdministration ? (
                      <Button
                        size="small"
                        color="inherit"
                        startIcon={<ManageAccountsIcon />}
                        variant={activeView === 'administration' ? 'outlined' : 'text'}
                        onClick={() => setActiveView('administration')}
                      >
                        管理
                      </Button>
                    ) : null}
                    {canReadAudit ? (
                      <Button
                        size="small"
                        color="inherit"
                        startIcon={<FactCheckIcon />}
                        variant={activeView === 'audit' ? 'outlined' : 'text'}
                        onClick={() => setActiveView('audit')}
                      >
                        审计
                      </Button>
                    ) : null}
                    <Tooltip title="修改密码">
                      <IconButton aria-label="修改密码" size="small" color="inherit" onClick={() => setPasswordDialogOpen(true)}>
                        <KeyIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Button size="small" color="inherit" onClick={() => void handleLogout()} disabled={authLoading}>
                      退出
                    </Button>
                  </>
                ) : (
                  <Chip size="small" variant="outlined" label="匿名开发模式" />
                )}
                {activeView === 'workbench' && canAccessWorkbench ? (
                  <Tooltip title="Refresh">
                    <IconButton size="small" onClick={() => void loadBasics()}>
                      <RefreshIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                ) : null}
              </Stack>
            ) : null}
          </Toolbar>
        </AppBar>

        {session ? (
          <PasswordChangeDialog
            open={passwordDialogOpen}
            onClose={() => setPasswordDialogOpen(false)}
            onChanged={handlePasswordChanged}
            onAuthenticationLost={handleAuthenticationLost}
          />
        ) : null}

        {authStatus === 'checking' ? (
          <AuthStatePanel>
            <CircularProgress size={28} />
            <Typography color="text.secondary">正在恢复会话…</Typography>
          </AuthStatePanel>
        ) : authStatus === 'error' ? (
          <AuthStatePanel>
            <Alert severity="error" sx={{ width: '100%' }}>
              {authError ?? '无法验证当前会话'}
            </Alert>
            <Button variant="outlined" onClick={() => void restoreAuthentication()}>
              重试
            </Button>
          </AuthStatePanel>
        ) : authStatus === 'unauthenticated' ? (
          <AuthenticationPanel
            loading={authLoading}
            error={authError}
            fieldErrors={authFieldErrors}
            notice={authNotice}
            onLogin={handleLogin}
            onRegister={handleRegistration}
            onModeChange={clearAuthFeedback}
          />
        ) : authStatus === 'authenticated' && activeView === 'history' && canUseHistory ? (
          <HistoryPanel onReuse={reuseHistory} onAuthenticationLost={handleAuthenticationLost} />
        ) : authStatus === 'authenticated' && activeView === 'audit' && canReadAudit ? (
          <AuditPanel onAuthenticationLost={handleAuthenticationLost} />
        ) : authStatus === 'authenticated' && activeView === 'administration' && canAccessAdministration ? (
          <AdministrationPanel
            session={session}
            canManageUsers={canManageUsers}
            canAssignRoles={canAssignRoles}
            canCreateImports={canCreateImports}
            onAuthenticationLost={reportRequestFailure}
            onSessionChanged={refreshCurrentSession}
          />
        ) : authStatus === 'authenticated' && !canAccessWorkbench ? (
          <AccessPendingPanel session={session} />
        ) : (
          <Box
            component="main"
            sx={{
              flex: 1,
              minHeight: 0,
              p: 1.5,
              display: 'grid',
              gridTemplateColumns: { xs: '1fr', lg: '310px minmax(480px, 1fr) 430px' },
              gap: 1.5,
            }}
          >
            <SchemaPanel
              columns={columns}
              distinct={distinct}
              canReadSchema={canReadSchema}
              canReadDistinct={canReadDistinct}
              onDistinct={handleDistinct}
            />

            <Stack spacing={1.5} minWidth={0} minHeight={0}>
              <Paper sx={{ p: 1.5 }}>
                <SectionTitle icon={<SmartToyIcon />} title="Ask" secondary="Natural language -> QuerySpec -> MySQL" />
                {!canRunAgent ? (
                  <Alert severity="info" sx={{ mt: 1.5 }}>
                    AI 分析需要同时具备 Agent、Schema、Distinct 和 Query 四项权限。
                  </Alert>
                ) : null}
                <TextField
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  multiline
                  minRows={4}
                  fullWidth
                  disabled={!canRunAgent}
                  sx={{ mt: 1.5 }}
                />
                <Stack direction="row" flexWrap="wrap" gap={0.75} sx={{ mt: 1.25 }}>
                  {sampleQuestions.map((sample) => (
                    <Chip key={sample} size="small" label={sample} disabled={!canRunAgent} onClick={() => setQuestion(sample)} />
                  ))}
                </Stack>
                <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
                  <Button startIcon={loading ? <CircularProgress size={16} color="inherit" /> : <PlayArrowIcon />} variant="contained" onClick={handleAsk} disabled={loading || !canRunAgent}>
                    Run Agent
                  </Button>
                  <Button variant="outlined" onClick={() => setQuerySpecText(JSON.stringify(sampleSpec, null, 2))}>
                    Load Sample QuerySpec
                  </Button>
                </Stack>
              </Paper>

              {error ? <Alert severity="error">{error}</Alert> : null}
              {askResult?.warnings?.map((warning) => (
                <Alert key={warning.code} severity="warning">
                  {warning.message}
                </Alert>
              ))}
              {askResult?.disclaimer ? (
                <Alert severity="info" icon={false}>
                  {askResult.disclaimer}
                </Alert>
              ) : null}

              <Paper sx={{ minHeight: 0, display: 'grid', gridTemplateRows: 'auto auto 280px minmax(240px, 1fr)' }}>
                <Box sx={{ p: 1.5, pb: 0 }}>
                  <SectionTitle
                    icon={<QueryStatsIcon />}
                    title="Result"
                    secondary={
                      askResult?.intent
                        ? `${askResult.intent.intent_type} · ${(askResult.intent.confidence * 100).toFixed(0)}%`
                        : queryResult
                          ? `${queryResult.row_count} rows in ${queryResult.query_time_ms} ms${queryResult.truncated ? ' · result truncated' : ''}`
                          : 'Waiting for query'
                    }
                    action={queryResult ? (
                      <Stack direction="row" spacing={0.5}>
                        <Tooltip title="本地导出 CSV">
                          <IconButton size="small" onClick={() => runExport(() => exportResultCsv(queryResult, exportTitle))}>
                            <DownloadIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title="本地导出图表 PNG">
                          <IconButton size="small" onClick={() => runExport(() => exportChartPng(exportTitle))}>
                            <ImageIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title="本地导出 HTML 报告">
                          <IconButton
                            size="small"
                            onClick={() => runExport(() => exportHtmlReport({
                              title: exportTitle,
                              question: askResult?.question ?? '结构化 QuerySpec 查询',
                              result: queryResult,
                              insight: askResult?.insight ?? null,
                              chartSpec: askResult?.chart_spec ?? null,
                              querySpec: executedQuerySpec,
                              warnings: askResult?.warnings ?? [],
                            }))}
                          >
                            <DescriptionIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                    ) : undefined}
                  />
                </Box>
                <InsightPanel insight={askResult?.insight ?? null} intent={askResult?.intent ?? null} />
                <ResultChart
                  result={queryResult}
                  chartSpec={askResult?.chart_spec ?? null}
                  querySpec={executedQuerySpec}
                />
                <ResultTable result={queryResult} />
              </Paper>
            </Stack>

            <Paper sx={{ minWidth: 0, minHeight: { xs: 520, lg: 0 }, display: 'flex', flexDirection: 'column' }}>
              <Box sx={{ p: 1.5, pb: 0 }}>
                <SectionTitle
                  icon={<SchemaIcon />}
                  title="QuerySpec"
                  secondary="Strict JSON, not SQL"
                  action={
                    <Tooltip title="Run QuerySpec">
                      <IconButton aria-label="Run QuerySpec" size="small" onClick={handleRunSpec} disabled={loading || !canRunQuery}>
                        <PlayArrowIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  }
                />
              </Box>
              <TextField
                value={querySpecText}
                onChange={(event) => setQuerySpecText(event.target.value)}
                multiline
                spellCheck={false}
                sx={{
                  flex: '1 1 48%',
                  m: 1.5,
                  '& textarea': {
                    fontFamily: '"Roboto Mono", Consolas, monospace',
                    fontSize: 12,
                    lineHeight: 1.5,
                  },
                }}
                minRows={12}
              />
              <Divider />
              <Box sx={{ p: 1.5 }}>
                <SectionTitle icon={<TableRowsIcon />} title="Execution" secondary="受控执行信息" />
                <Box
                  component="pre"
                  sx={{
                    mt: 1,
                    mb: 0,
                    maxHeight: 260,
                    overflow: 'auto',
                    p: 1.25,
                    borderRadius: 1,
                    bgcolor: '#101828',
                    color: '#f8fafc',
                    fontSize: 12,
                    lineHeight: 1.45,
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {executionText(askResult, queryResult)}
                </Box>
              </Box>
            </Paper>
          </Box>
        )}
      </Box>
    </ThemeProvider>
  );
}

function AuthStatePanel({ children }: { children: ReactNode }) {
  return (
    <Box component="main" sx={{ flex: 1, display: 'grid', placeItems: 'center', p: 2 }}>
      <Stack spacing={2} alignItems="center" sx={{ width: '100%', maxWidth: 460 }}>
        {children}
      </Stack>
    </Box>
  );
}

function AccessPendingPanel({ session }: { session: AuthSession | null }) {
  return (
    <AuthStatePanel>
      <Paper sx={{ width: '100%', p: 3 }}>
        <Stack spacing={2}>
          <Box>
            <Chip label="账号已激活" color="success" size="small" sx={{ mb: 1.5 }} />
            <Typography variant="h2">分析权限待开通</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
              {session?.user.display_name || session?.user.email || '当前账号'}已加入
              {session?.organization.name ? `“${session.organization.name}”` : '当前组织'}。
            </Typography>
          </Box>
          <Alert severity="info">
            账号已激活，等待管理员分配分析权限和机构数据范围。
          </Alert>
          <Box>
            <Typography variant="body2" fontWeight={700}>
              管理员需要完成：
            </Typography>
            <Box component="ul" sx={{ mt: 1, mb: 0, pl: 2.5, color: 'text.secondary' }}>
              <li>
                <Typography variant="body2">分配具备 Schema 与分析能力的角色</Typography>
              </li>
              <li>
                <Typography variant="body2">配置允许访问的机构数据范围</Typography>
              </li>
            </Box>
          </Box>
          <Divider />
          <Typography variant="caption" color="text.secondary">
            为保护医疗数据，权限开通前不会加载字段结构或发起分析请求。完成授权后，请退出并重新登录以刷新会话权限。
          </Typography>
        </Stack>
      </Paper>
    </AuthStatePanel>
  );
}

function AdministrationPanel({
  session,
  canManageUsers,
  canAssignRoles,
  canCreateImports,
  onAuthenticationLost,
  onSessionChanged,
}: {
  session: AuthSession | null;
  canManageUsers: boolean;
  canAssignRoles: boolean;
  canCreateImports: boolean;
  onAuthenticationLost: (error: unknown) => void;
  onSessionChanged: () => Promise<void>;
}) {
  const [members, setMembers] = useState<AdminMember[]>([]);
  const [roles, setRoles] = useState<AdminRole[]>([]);
  const [facilities, setFacilities] = useState<AdminFacility[]>([]);
  const [selectedMember, setSelectedMember] = useState<AdminMember | null>(null);
  const [statusMember, setStatusMember] = useState<AdminMember | null>(null);
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[]>([]);
  const [selectedFacilityIds, setSelectedFacilityIds] = useState<string[]>([]);
  const [invitationEmail, setInvitationEmail] = useState('');
  const [invitationHours, setInvitationHours] = useState('24');
  const [issuedInvitation, setIssuedInvitation] = useState<{
    token: string;
    expiresAt: string;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const canViewMembers = canManageUsers || canAssignRoles;

  const handleError = useCallback(
    (cause: unknown) => {
      if (isAuthenticationLost(cause)) {
        onAuthenticationLost(cause);
      }
      setError(cause instanceof Error ? cause.message : String(cause));
    },
    [onAuthenticationLost],
  );

  const handleRoleAuthenticationLost = useCallback(() => {
    onAuthenticationLost(new ApiError({
      status: 401,
      code: 'AUTHENTICATION_REQUIRED',
      message: '当前会话已失效，请重新登录',
    }));
  }, [onAuthenticationLost]);

  const loadAdministration = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [memberItems, roleItems, facilityItems] = await Promise.all([
        canViewMembers ? collectAdminPages(listAdminMembers) : Promise.resolve([]),
        canAssignRoles ? collectAdminPages(listAdminRoles) : Promise.resolve([]),
        canAssignRoles || canCreateImports
          ? collectAdminPages(listAdminFacilities)
          : Promise.resolve([]),
      ]);
      setMembers(memberItems);
      setRoles(roleItems);
      setFacilities(facilityItems);
    } catch (cause) {
      handleError(cause);
    } finally {
      setLoading(false);
    }
  }, [canAssignRoles, canCreateImports, canViewMembers, handleError]);

  useEffect(() => {
    void loadAdministration();
  }, [loadAdministration]);

  const openMember = (member: AdminMember) => {
    setSelectedMember(member);
    setSelectedRoleIds(member.roles.map((role) => role.id));
    setSelectedFacilityIds(member.facilities.map((facility) => facility.id));
    setError(null);
    setNotice(null);
  };

  const updateMemberInList = (member: AdminMember) => {
    setMembers((current) =>
      current.map((item) => (item.membership_id === member.membership_id ? member : item)),
    );
  };

  const saveMemberAccess = async () => {
    if (!selectedMember || !canAssignRoles) return;
    const isCurrentMember =
      selectedMember.membership_id === session?.organization.membership_id;
    let accessChanged = false;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      let updated = selectedMember;
      const currentRoleIds = updated.roles.map((role) => role.id);
      if (!sameIdentifierSet(currentRoleIds, selectedRoleIds)) {
        updated = await replaceAdminMemberRoles(
          updated.membership_id,
          selectedRoleIds,
          updated.version,
        );
        updateMemberInList(updated);
        setSelectedMember(updated);
        accessChanged = true;
      }
      const currentFacilityIds = updated.facilities.map((facility) => facility.id);
      if (!sameIdentifierSet(currentFacilityIds, selectedFacilityIds)) {
        updated = await replaceAdminMemberFacilityScope(
          updated.membership_id,
          selectedFacilityIds,
          updated.version,
        );
        updateMemberInList(updated);
        accessChanged = true;
      }
      setNotice(`已更新 ${updated.display_name || updated.email} 的角色和机构范围。`);
      setSelectedMember(null);
      if (accessChanged && isCurrentMember) {
        await onSessionChanged();
      }
    } catch (cause) {
      await loadAdministration();
      let sessionRefreshFailure: unknown = null;
      if (accessChanged && isCurrentMember) {
        try {
          await onSessionChanged();
        } catch (refreshCause) {
          sessionRefreshFailure = refreshCause;
        }
      }
      if (accessChanged && !(cause instanceof ApiError && cause.status === 401)) {
        if (sessionRefreshFailure instanceof ApiError && sessionRefreshFailure.status === 401) {
          handleError(sessionRefreshFailure);
          return;
        }
        setSelectedMember(null);
        const message = cause instanceof Error ? cause.message : String(cause);
        const refreshMessage = sessionRefreshFailure
          ? `；当前会话刷新失败：${
              sessionRefreshFailure instanceof Error
                ? sessionRefreshFailure.message
                : String(sessionRefreshFailure)
            }`
          : '';
        setError(`部分权限变更可能已提交，机构范围未确认完成：${message}${refreshMessage}`);
      } else {
        handleError(cause);
      }
    } finally {
      setSaving(false);
    }
  };

  const changeMemberStatus = async (member: AdminMember) => {
    if (
      !canManageUsers ||
      (member.membership_status !== 'active' && member.membership_status !== 'suspended')
    ) return;
    const nextStatus = member.membership_status === 'active' ? 'suspended' : 'active';
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await updateAdminMemberStatus(
        member.membership_id,
        nextStatus,
        member.version,
      );
      updateMemberInList(updated);
      setNotice(nextStatus === 'active' ? '成员已恢复。' : '成员已停用，现有会话已失效。');
      setStatusMember(null);
    } catch (cause) {
      handleError(cause);
    } finally {
      setSaving(false);
    }
  };

  const issueInvitation = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canManageUsers || !invitationEmail.trim()) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    setIssuedInvitation(null);
    try {
      const hours = Number(invitationHours);
      const invitation = await issueAdminInvitation(
        invitationEmail,
        Number.isFinite(hours) ? hours : 24,
      );
      setIssuedInvitation({ token: invitation.token, expiresAt: invitation.expires_at });
      setInvitationEmail('');
      setNotice('邀请已创建。请立即通过受信任渠道发送，关闭后无法再次查看原令牌。');
      await loadAdministration();
    } catch (cause) {
      handleError(cause);
    } finally {
      setSaving(false);
    }
  };

  const syncFacilities = async () => {
    if (!canCreateImports && !canAssignRoles) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await syncAdminFacilities();
      setNotice(
        `机构目录同步完成：新增 ${result.created_count} 个，已有 ${result.existing_count} 个。`,
      );
      const facilityItems = await collectAdminPages(listAdminFacilities);
      setFacilities(facilityItems);
    } catch (cause) {
      handleError(cause);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Box component="main" sx={{ flex: 1, minHeight: 0, p: 2 }}>
      <Stack spacing={2} sx={{ maxWidth: 1500, mx: 'auto' }}>
        <Paper sx={{ p: 2 }}>
          <Stack
            direction={{ xs: 'column', md: 'row' }}
            alignItems={{ xs: 'flex-start', md: 'center' }}
            justifyContent="space-between"
            gap={2}
          >
            <Box>
              <Typography variant="h2">组织管理</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                {session?.organization.name} · 用户、角色和机构数据范围
              </Typography>
            </Box>
            <Stack direction="row" spacing={1}>
              {(canCreateImports || canAssignRoles) ? (
                <Button
                  variant="outlined"
                  startIcon={<StorageIcon />}
                  onClick={() => void syncFacilities()}
                  disabled={saving}
                >
                  同步机构目录
                </Button>
              ) : null}
              <Button
                variant="outlined"
                startIcon={<RefreshIcon />}
                onClick={() => void loadAdministration()}
                disabled={loading || saving}
              >
                刷新
              </Button>
            </Stack>
          </Stack>
        </Paper>

        {error ? <Alert severity="error">{error}</Alert> : null}
        {notice ? <Alert severity="success">{notice}</Alert> : null}
        {issuedInvitation ? (
          <Alert severity="warning">
            <Typography variant="body2" fontWeight={700}>
              一次性邀请码（有效至 {formatDateTime(issuedInvitation.expiresAt)}）
            </Typography>
            <Typography
              component="code"
              sx={{ display: 'block', mt: 1, overflowWrap: 'anywhere', userSelect: 'all' }}
            >
              {issuedInvitation.token}
            </Typography>
          </Alert>
        ) : null}

        {canManageUsers ? (
          <Paper component="form" onSubmit={issueInvitation} sx={{ p: 2 }}>
            <Typography variant="h2">邀请成员</Typography>
            <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ mt: 1.5 }}>
              <TextField
                label="邮箱"
                type="email"
                value={invitationEmail}
                onChange={(event) => setInvitationEmail(event.target.value)}
                required
                fullWidth
              />
              <TextField
                label="有效小时数"
                type="number"
                value={invitationHours}
                onChange={(event) => setInvitationHours(event.target.value)}
                inputProps={{ min: 1, max: 168 }}
                sx={{ width: { xs: '100%', md: 180 } }}
              />
              <Button
                type="submit"
                variant="contained"
                disabled={saving || !invitationEmail.trim()}
                sx={{ minWidth: 130 }}
              >
                创建邀请
              </Button>
            </Stack>
          </Paper>
        ) : null}

        {canAssignRoles ? (
          <RoleManagementPanel
            roles={roles}
            onChanged={loadAdministration}
            onAuthenticationLost={handleRoleAuthenticationLost}
          />
        ) : null}

        {canViewMembers ? <Paper sx={{ overflow: 'hidden' }}>
          <Box sx={{ p: 2, pb: 1 }}>
            <Typography variant="h2">成员</Typography>
            <Typography variant="caption" color="text.secondary">
              共 {members.length} 个；空机构范围表示禁止访问任何医疗数据。
            </Typography>
          </Box>
          {loading ? (
            <Box sx={{ display: 'grid', placeItems: 'center', minHeight: 220 }}>
              <CircularProgress size={28} />
            </Box>
          ) : (
            <TableContainer sx={{ maxHeight: 'calc(100vh - 390px)' }}>
              <Table stickyHeader size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>用户</TableCell>
                    <TableCell>状态</TableCell>
                    <TableCell>角色</TableCell>
                    <TableCell>机构范围</TableCell>
                    <TableCell align="right">操作</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {members.map((member) => (
                    <TableRow key={member.membership_id} hover>
                      <TableCell>
                        <Typography variant="body2" fontWeight={700}>
                          {member.display_name || '未激活成员'}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {member.email}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          label={memberStatusLabel(member.membership_status)}
                          color={member.membership_status === 'active' ? 'success' : 'default'}
                          variant={member.membership_status === 'active' ? 'filled' : 'outlined'}
                        />
                      </TableCell>
                      <TableCell>
                        <Stack direction="row" gap={0.5} flexWrap="wrap">
                          {member.roles.length ? member.roles.map((role) => (
                            <Chip key={role.id} size="small" label={role.name} />
                          )) : <Typography variant="caption" color="text.secondary">未分配</Typography>}
                        </Stack>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2">
                          {member.facilities.length ? `${member.facilities.length} 个机构` : '禁止访问'}
                        </Typography>
                      </TableCell>
                      <TableCell align="right">
                        <Stack direction="row" spacing={0.75} justifyContent="flex-end">
                          {canAssignRoles ? (
                            <Button size="small" onClick={() => openMember(member)} disabled={saving}>
                              配置权限
                            </Button>
                          ) : null}
                          {member.membership_id === session?.organization.membership_id ? (
                            <Chip size="small" variant="outlined" label="当前账号" />
                          ) : canManageUsers && ['active', 'suspended'].includes(member.membership_status) ? (
                            <Button
                              size="small"
                              color={member.membership_status === 'active' ? 'error' : 'primary'}
                              onClick={() => setStatusMember(member)}
                              disabled={saving}
                            >
                              {member.membership_status === 'active' ? '停用' : '恢复'}
                            </Button>
                          ) : null}
                        </Stack>
                      </TableCell>
                    </TableRow>
                  ))}
                  {!members.length ? (
                    <TableRow>
                      <TableCell colSpan={5} align="center" sx={{ py: 6, color: 'text.secondary' }}>
                        暂无组织成员
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </Paper> : null}
      </Stack>

      <Dialog
        open={selectedMember !== null}
        onClose={() => !saving && setSelectedMember(null)}
        fullWidth
        maxWidth="md"
      >
        <DialogTitle>
          配置 {selectedMember?.display_name || selectedMember?.email || '成员'}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2.5}>
            <Box>
              <Typography variant="body2" fontWeight={700}>角色</Typography>
              <Stack direction="row" flexWrap="wrap" gap={0.5} sx={{ mt: 0.75 }}>
                {roles.map((role) => (
                  <FormControlLabel
                    key={role.id}
                    control={(
                      <Checkbox
                        checked={selectedRoleIds.includes(role.id)}
                        onChange={() => setSelectedRoleIds((current) => toggleIdentifier(current, role.id))}
                      />
                    )}
                    label={role.name}
                  />
                ))}
              </Stack>
            </Box>
            <Divider />
            <Box>
              <Stack direction="row" alignItems="center" justifyContent="space-between" gap={1}>
                <Box>
                  <Typography variant="body2" fontWeight={700}>允许访问的机构</Typography>
                  <Typography variant="caption" color="text.secondary">
                    未勾选任何机构时，该成员不能执行医疗数据查询。
                  </Typography>
                </Box>
                {facilities.length ? (
                  <Stack direction="row" spacing={0.5}>
                    <Button
                      size="small"
                      onClick={() => setSelectedFacilityIds(
                        facilities
                          .filter((facility) => facility.status === 'active')
                          .map((facility) => facility.id),
                      )}
                    >
                      全部选择
                    </Button>
                    <Button size="small" onClick={() => setSelectedFacilityIds([])}>
                      清空
                    </Button>
                  </Stack>
                ) : null}
              </Stack>
              <Box sx={{ mt: 1, maxHeight: 330, overflow: 'auto', display: 'grid', gridTemplateColumns: { xs: '1fr', md: '1fr 1fr' } }}>
                {facilities.map((facility) => (
                  <FormControlLabel
                    key={facility.id}
                    control={(
                      <Checkbox
                        checked={selectedFacilityIds.includes(facility.id)}
                        disabled={
                          facility.status !== 'active' &&
                          !selectedFacilityIds.includes(facility.id)
                        }
                        onChange={() => setSelectedFacilityIds((current) => toggleIdentifier(current, facility.id))}
                      />
                    )}
                    label={`${facility.display_name || facility.facility_key} (${facility.facility_key})${
                      facility.status === 'active' ? '' : '（已停用）'
                    }`}
                  />
                ))}
                {!facilities.length ? (
                  <Alert severity="info" sx={{ gridColumn: '1 / -1' }}>
                    机构目录为空，请先点击“同步机构目录”。
                  </Alert>
                ) : null}
              </Box>
            </Box>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSelectedMember(null)} disabled={saving}>取消</Button>
          <Button variant="contained" onClick={() => void saveMemberAccess()} disabled={saving}>
            {saving ? <CircularProgress size={18} color="inherit" /> : '保存权限'}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog
        open={statusMember !== null}
        onClose={() => !saving && setStatusMember(null)}
        fullWidth
        maxWidth="xs"
      >
        <DialogTitle>
          {statusMember?.membership_status === 'active' ? '确认停用成员' : '确认恢复成员'}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {statusMember?.membership_status === 'active'
              ? `停用 ${statusMember.display_name || statusMember.email} 后，其现有会话会立即失效，且无法访问组织数据。`
              : `恢复 ${statusMember?.display_name || statusMember?.email} 的组织成员资格？`}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setStatusMember(null)} disabled={saving}>取消</Button>
          <Button
            variant="contained"
            color={statusMember?.membership_status === 'active' ? 'error' : 'primary'}
            onClick={() => statusMember && void changeMemberStatus(statusMember)}
            disabled={saving}
          >
            {saving ? <CircularProgress size={18} color="inherit" /> : '确认'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

async function collectAdminPages<T>(
  loader: (cursor?: string) => Promise<{ items: T[]; next_cursor: string | null }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < 20; page += 1) {
    const payload = await loader(cursor);
    items.push(...payload.items);
    if (!payload.next_cursor) return items;
    cursor = payload.next_cursor;
  }
  throw new ApiError({
    status: 500,
    code: 'ADMIN_PAGINATION_LIMIT',
    message: '管理数据页数超过当前界面上限，请缩小查询范围',
  });
}

function toggleIdentifier(current: string[], value: string): string[] {
  return current.includes(value) ? current.filter((item) => item !== value) : [...current, value];
}

function sameIdentifierSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const normalized = new Set(left);
  return right.every((value) => normalized.has(value));
}

function memberStatusLabel(status: AdminMember['membership_status']): string {
  const labels: Record<AdminMember['membership_status'], string> = {
    invited: '待注册',
    active: '正常',
    suspended: '已停用',
    removed: '已移除',
  };
  return labels[status];
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function AuthenticationPanel({
  loading,
  error,
  fieldErrors,
  notice,
  onLogin,
  onRegister,
  onModeChange,
}: {
  loading: boolean;
  error: string | null;
  fieldErrors: AuthFieldError[];
  notice: string | null;
  onLogin: (email: string, password: string, organizationId: string) => Promise<void>;
  onRegister: (credentials: RegistrationCredentials) => Promise<string | null>;
  onModeChange: () => void;
}) {
  const [mode, setMode] = useState<'login' | 'register' | 'forgot' | 'reset'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [organizationId, setOrganizationId] = useState('');
  const [invitationToken, setInvitationToken] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [passwordConfirmation, setPasswordConfirmation] = useState('');
  const [resetToken, setResetToken] = useState('');
  const [clientError, setClientError] = useState<string | null>(null);
  const [flowNotice, setFlowNotice] = useState<string | null>(null);
  const [flowLoading, setFlowLoading] = useState(false);
  const busy = loading || flowLoading;

  const changeMode = (_event: React.SyntheticEvent, nextMode: 'login' | 'register') => {
    setMode(nextMode);
    setPassword('');
    setPasswordConfirmation('');
    setInvitationToken('');
    setResetToken('');
    setClientError(null);
    setFlowNotice(null);
    onModeChange();
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setClientError(null);
    setFlowNotice(null);
    if (mode === 'login') {
      if (!email.trim() || !password) return;
      await onLogin(email.trim(), password, organizationId);
      return;
    }

    if (mode === 'forgot') {
      if (!email.trim()) return;
      setFlowLoading(true);
      try {
        const result = await requestPasswordReset(email, organizationId);
        if (result.reset_token) {
          setResetToken(result.reset_token);
          if (result.organization_id) setOrganizationId(result.organization_id);
          setMode('reset');
          setFlowNotice('演示环境已生成一次性重置令牌，请设置新密码。');
        } else {
          setFlowNotice('如果账号信息匹配，密码重置说明将通过已配置的渠道发送。');
        }
      } catch (cause) {
        setClientError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setFlowLoading(false);
      }
      return;
    }

    if (mode === 'reset') {
      if (!resetToken.trim() || !email.trim() || !organizationId.trim() || !password) return;
      if (password.length < 12) {
        setClientError('密码至少需要 12 个字符。');
        return;
      }
      if (password !== passwordConfirmation) {
        setClientError('两次输入的密码不一致。');
        return;
      }
      setFlowLoading(true);
      try {
        await resetPassword(resetToken, email, organizationId, password);
        setMode('login');
        setResetToken('');
        setPassword('');
        setPasswordConfirmation('');
        setFlowNotice('密码已重置，请使用新密码登录。');
      } catch (cause) {
        setClientError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setFlowLoading(false);
      }
      return;
    }

    if (!invitationToken.trim() || !email.trim() || !displayName.trim() || !password) return;
    if (password.length < 12) {
      setClientError('密码至少需要 12 个字符。');
      return;
    }
    if (password !== passwordConfirmation) {
      setClientError('两次输入的密码不一致。');
      return;
    }
    const registeredOrganizationId = await onRegister({
      invitation_token: invitationToken,
      email,
      display_name: displayName,
      password,
    });
    if (registeredOrganizationId) {
      setMode('login');
      setInvitationToken('');
      setDisplayName('');
      setPassword('');
      setPasswordConfirmation('');
      setOrganizationId(registeredOrganizationId);
    }
  };

  const registerFormComplete = Boolean(
    invitationToken.trim() &&
      email.trim() &&
      displayName.trim() &&
      password.length >= 12 &&
      passwordConfirmation,
  );

  return (
    <AuthStatePanel>
      <Paper component="form" onSubmit={handleSubmit} sx={{ width: '100%', p: 3 }}>
        <Stack spacing={2}>
          <Box>
            <Typography variant="h2">
              {mode === 'forgot' ? '找回密码' : mode === 'reset' ? '设置新密码' : '访问工作台'}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {mode === 'forgot' || mode === 'reset'
                ? '重置令牌具有时效且只能使用一次。'
                : '使用组织账号登录，或通过管理员发放的一次性邀请码注册。'}
            </Typography>
          </Box>
          {mode === 'login' || mode === 'register' ? (
            <Tabs value={mode} onChange={changeMode} variant="fullWidth" aria-label="登录或注册">
              <Tab value="login" label="登录" disabled={busy} />
              <Tab value="register" label="注册" disabled={busy} />
            </Tabs>
          ) : (
            <Button
              size="small"
              onClick={() => {
                setMode('login');
                setPassword('');
                setPasswordConfirmation('');
                setResetToken('');
                setClientError(null);
                setFlowNotice(null);
                onModeChange();
              }}
              disabled={busy}
            >
              返回登录
            </Button>
          )}
          {notice ? <Alert severity="success">{notice}</Alert> : null}
          {flowNotice ? <Alert severity="success">{flowNotice}</Alert> : null}
          {error ? <Alert severity="error">{error}</Alert> : null}
          {fieldErrors.length ? (
            <Alert severity="error">
              {fieldErrors.map((fieldError) => (
                <Typography key={`${fieldError.field}:${fieldError.message}`} variant="body2">
                  {fieldError.field}: {fieldError.message}
                </Typography>
              ))}
            </Alert>
          ) : null}
          {clientError ? <Alert severity="warning">{clientError}</Alert> : null}
          {mode === 'register' ? (
            <TextField
              label="邀请码"
              value={invitationToken}
              onChange={(event) => setInvitationToken(event.target.value)}
              autoComplete="off"
              required
              autoFocus
              fullWidth
              helperText="邀请码由组织管理员发放，且只能使用一次。"
            />
          ) : null}
          <TextField
            label="邮箱"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="username"
            required
            autoFocus={mode === 'login' || mode === 'forgot'}
            fullWidth
          />
          {mode === 'register' ? (
            <TextField
              label="显示名称"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              autoComplete="name"
              required
              fullWidth
              helperText="首次激活时使用该名称；已有账号加入新组织时保持原名称。"
            />
          ) : null}
          {mode === 'reset' ? (
            <TextField
              label="重置令牌"
              type="password"
              value={resetToken}
              onChange={(event) => setResetToken(event.target.value)}
              autoComplete="off"
              required
              fullWidth
            />
          ) : null}
          {mode !== 'forgot' ? (
            <TextField
              label={mode === 'reset' ? '新密码' : '密码'}
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              inputProps={mode === 'login' ? undefined : { minLength: 12 }}
              required
              fullWidth
              helperText={mode === 'login' ? undefined : '至少 12 个字符；最终规则以服务端校验为准。'}
            />
          ) : null}
          {mode === 'register' || mode === 'reset' ? (
            <>
              <TextField
                label="确认密码"
                type="password"
                value={passwordConfirmation}
                onChange={(event) => setPasswordConfirmation(event.target.value)}
                autoComplete="new-password"
                inputProps={{ minLength: 12 }}
                required
                fullWidth
                error={Boolean(passwordConfirmation && password !== passwordConfirmation)}
                helperText={
                  passwordConfirmation && password !== passwordConfirmation ? '两次输入的密码不一致。' : undefined
                }
              />
              {mode === 'register' ? (
                <Alert severity="info" icon={false}>
                  注册仅创建组织成员账号，不会自动授予医疗数据查询或管理权限。
                </Alert>
              ) : null}
            </>
          ) : null}
          {mode !== 'register' ? (
            <TextField
              label={`组织 ID${mode === 'reset' ? '' : '（可选）'}`}
              value={organizationId}
              onChange={(event) => setOrganizationId(event.target.value)}
              autoComplete="off"
              required={mode === 'reset'}
              fullWidth
            />
          ) : null}
          {mode === 'forgot' ? (
            <Button
              size="small"
              onClick={() => {
                setMode('reset');
                setClientError(null);
                setFlowNotice(null);
              }}
              disabled={busy}
            >
              我已有重置令牌
            </Button>
          ) : null}
          {mode === 'login' ? (
            <Button
              size="small"
              onClick={() => {
                setMode('forgot');
                setPassword('');
                setClientError(null);
                setFlowNotice(null);
                onModeChange();
              }}
              disabled={busy}
            >
              忘记密码？
            </Button>
          ) : null}
          <Button
            type="submit"
            variant="contained"
            disabled={
              busy ||
              (mode === 'login'
                ? !email.trim() || !password
                : mode === 'register'
                  ? !registerFormComplete
                  : mode === 'forgot'
                    ? !email.trim()
                    : !resetToken.trim() || !email.trim() || !organizationId.trim() || password.length < 12 || !passwordConfirmation)
            }
          >
            {busy ? (
              <CircularProgress size={20} color="inherit" />
            ) : mode === 'login' ? (
              '登录'
            ) : mode === 'register' ? (
              '注册账号'
            ) : mode === 'forgot' ? (
              '申请重置'
            ) : (
              '重置密码'
            )}
          </Button>
        </Stack>
      </Paper>
    </AuthStatePanel>
  );
}

function StatusChip({ icon, label, ok }: { icon: ReactElement; label: string; ok: boolean }) {
  return <Chip size="small" icon={icon} label={label} color={ok ? 'success' : 'default'} variant={ok ? 'filled' : 'outlined'} />;
}

function SectionTitle({ icon, title, secondary, action }: { icon: ReactNode; title: string; secondary?: string; action?: ReactNode }) {
  return (
    <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
      <Stack direction="row" alignItems="center" spacing={1} minWidth={0}>
        <Box sx={{ color: 'primary.main', display: 'flex' }}>{icon}</Box>
        <Box minWidth={0}>
          <Typography variant="h2">{title}</Typography>
          {secondary ? (
            <Typography variant="caption" color="text.secondary" noWrap>
              {secondary}
            </Typography>
          ) : null}
        </Box>
      </Stack>
      {action}
    </Stack>
  );
}

function SchemaPanel({
  columns,
  distinct,
  canReadSchema,
  canReadDistinct,
  onDistinct,
}: {
  columns: SchemaColumn[];
  distinct: DistinctPayload | null;
  canReadSchema: boolean;
  canReadDistinct: boolean;
  onDistinct: (field: string) => void;
}) {
  return (
    <Paper sx={{ minHeight: { xs: 360, lg: 0 }, display: 'flex', flexDirection: 'column' }}>
      <Box sx={{ p: 1.5 }}>
        <SectionTitle
          icon={<SchemaIcon />}
          title="Schema"
          secondary={canReadSchema ? `${columns.length} allowlisted fields` : '当前角色没有 Schema 权限'}
        />
        <Stack direction="row" flexWrap="wrap" gap={0.75} sx={{ mt: 1.5 }}>
          {['AgeGroup', 'Gender', 'AdmissionType', 'PaymentTypology1', 'Race'].map((field) => (
            <Chip
              key={field}
              size="small"
              label={field}
              disabled={!canReadDistinct}
              onClick={() => onDistinct(field)}
            />
          ))}
        </Stack>
        <Box sx={{ mt: 1.5, p: 1, minHeight: 66, maxHeight: 120, overflow: 'auto', border: '1px solid rgba(16,24,40,0.12)', borderRadius: 1, bgcolor: '#fcfcfd' }}>
          <Typography variant="caption" color="text.secondary">
            {distinct
              ? `${distinct.field}: ${distinct.values.join(', ')}${distinct.truncated ? ' ...' : ''}`
              : canReadDistinct
                ? 'Click a common categorical field above to inspect privacy-safe distinct values.'
                : '当前角色没有去重值读取权限。'}
          </Typography>
        </Box>
      </Box>
      <Divider />
      <List dense sx={{ overflow: 'auto', flex: 1, py: 0 }}>
        {columns.map((column) => (
          <ListItemButton key={column.name} component="div" sx={{ alignItems: 'flex-start', borderBottom: '1px solid rgba(16,24,40,0.08)' }}>
            <ListItemText
              primary={
                <Stack direction="row" justifyContent="space-between" spacing={1}>
                  <Typography variant="body2" fontWeight={700}>
                    {column.name}
                  </Typography>
                  <Typography variant="caption" color={column.numeric ? 'secondary.main' : 'primary.main'}>
                    {column.type}
                  </Typography>
                </Stack>
              }
              secondary={column.description}
              secondaryTypographyProps={{ fontSize: 11, lineHeight: 1.35 }}
            />
          </ListItemButton>
        ))}
      </List>
    </Paper>
  );
}

function InsightPanel({ insight, intent }: { insight: AgentInsight | null; intent: AgentIntent | null }) {
  if (!insight && !intent) {
    return <Box sx={{ px: 1.5, py: 1 }} />;
  }
  return (
    <Box sx={{ px: 1.5, py: 1 }}>
      {insight ? (
        <Stack spacing={0.75}>
          <Typography variant="body2" fontWeight={700}>
            {insight.summary}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {insight.chart_reading}
          </Typography>
          {insight.observations.length ? (
            <Stack direction="row" flexWrap="wrap" gap={0.75}>
              {insight.observations.slice(0, 3).map((item) => (
                <Chip key={item} size="small" label={item} />
              ))}
            </Stack>
          ) : null}
        </Stack>
      ) : intent ? (
        <Typography variant="caption" color="text.secondary">
          {intent.reason}
        </Typography>
      ) : null}
    </Box>
  );
}

function ResultChart({
  result,
  chartSpec,
  querySpec,
}: {
  result: QueryResult | null;
  chartSpec: ChartSpec | null;
  querySpec: Record<string, unknown> | null;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartColumns = result?.columns ?? [];
  const rows = useMemo(() => result?.rows ?? [], [result]);

  useEffect(() => {
    if (!ref.current) return;
    const instance = echarts.init(ref.current);
    const fields = resolveChartFields(result, chartSpec);
    const { xField, yField } = fields;
    const chartable = Boolean(result && xField && yField && rows.length > 0 && fields.chartType !== 'table');
    ref.current.dataset.chartable = chartable ? 'true' : 'false';
    if (!chartable || !result || !xField || !yField) {
      instance.setOption({
        title: { text: 'No chartable result yet', left: 'center', top: 'middle', textStyle: { color: '#667085', fontSize: 13, fontWeight: 400 } },
      });
    } else {
      instance.setOption(
        buildChartOption(rows, { ...fields, xField, yField }, chartSpec, result, querySpec),
        true,
      );
    }
    let resizeFrame: number | null = null;
    const resize = () => {
      if (resizeFrame !== null) return;
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        instance.resize();
      });
    };
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      instance.dispose();
    };
  }, [chartColumns, chartSpec, querySpec, result, rows]);

  return <Box id={CHART_ELEMENT_ID} data-chartable="false" ref={ref} sx={{ minHeight: 0, borderBottom: '1px solid rgba(16,24,40,0.12)' }} />;
}

function resolveChartFields(result: QueryResult | null, chartSpec: ChartSpec | null) {
  const rows = result?.rows ?? [];
  const columns = result?.columns ?? [];
  const firstRow = rows[0] ?? {};
  const numericColumns = columns.filter((column) => typeof firstRow[column] === 'number');
  const labelColumns = columns.filter((column) => typeof firstRow[column] !== 'number');
  const xField = pickExisting(columns, chartSpec?.x_field) ?? labelColumns[0] ?? columns[0];
  const yField = pickExisting(columns, chartSpec?.y_field) ?? numericColumns.find((column) => column !== xField);
  const seriesField = pickExisting(columns, chartSpec?.series_field);
  const chartType = chartSpec?.chart_type ?? (seriesField ? 'grouped_bar' : 'bar');
  return { chartType, xField, yField, seriesField };
}

function pickExisting(columns: string[], field?: string | null) {
  return field && columns.includes(field) ? field : undefined;
}

function buildChartOption(
  rows: QueryResult['rows'],
  fields: { chartType: string; xField: string; yField: string; seriesField?: string },
  chartSpec: ChartSpec | null,
  result: Pick<QueryResult, 'row_count' | 'truncated'>,
  querySpec: Record<string, unknown> | null,
): echarts.EChartsOption {
  const { chartType, xField, yField, seriesField } = fields;
  const title = chartSpec?.title ?? yField;
  const unit = metricUnit(yField);
  const scope = summarizeQueryScope(querySpec);
  const context = `${xField} → ${yField} (${unit}) · ${scope} · ${result.row_count} returned rows${result.truncated ? ' · truncated' : ''}`;
  const titleOption = {
    text: title,
    subtext: context,
    left: 16,
    top: 8,
    textStyle: { color: '#344054', fontSize: 13 },
    subtextStyle: { color: '#667085', fontSize: 10 },
  };
  const xAxisName = { name: xField, nameLocation: 'middle' as const, nameGap: 30 };
  const yAxisName = { name: `${yField} (${unit})`, nameLocation: 'middle' as const, nameGap: 52 };

  if (chartType === 'number') {
    return {
      title: { ...titleOption, left: 'center', textStyle: { color: '#344054', fontSize: 14 } },
      graphic: {
        type: 'text',
        left: 'center',
        top: 'middle',
        style: { text: formatCell(rows[0]?.[yField]), fill: '#101828', fontSize: 34, fontWeight: 700 },
      },
    };
  }

  if (chartType === 'pie') {
    return {
      title: titleOption,
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll', bottom: 0, textStyle: { color: '#667085' } },
      series: [
        {
          type: 'pie',
          radius: ['38%', '68%'],
          center: ['50%', '48%'],
          data: rows.map((row) => ({ name: String(row[xField]), value: Number(row[yField] ?? 0) })),
        },
      ],
    };
  }

  if (seriesField || chartType === 'grouped_bar') {
    const xValues = uniqueValues(rows.map((row) => String(row[xField])));
    const seriesValues = uniqueValues(rows.map((row) => String(row[seriesField ?? xField])));
    return {
      title: titleOption,
      grid: { left: 72, right: 24, top: 72, bottom: 58 },
      tooltip: { trigger: 'axis' },
      legend: { top: 28, textStyle: { color: '#667085' } },
      xAxis: { type: 'category', data: xValues, axisLabel: { color: '#344054' }, ...xAxisName },
      yAxis: { type: 'value', axisLabel: { color: '#667085' }, ...yAxisName },
      series: seriesValues.map((value) => ({
        name: value,
        type: chartType === 'line' ? 'line' : 'bar',
        smooth: chartType === 'line',
        data: xValues.map((xValue) => {
          const row = rows.find((candidate) => String(candidate[xField]) === xValue && String(candidate[seriesField ?? xField]) === value);
          return Number(row?.[yField] ?? 0);
        }),
      })),
    };
  }

  return {
    title: titleOption,
    grid: { left: 72, right: 24, top: 72, bottom: 58 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: rows.map((row) => String(row[xField])), axisLabel: { color: '#344054' }, ...xAxisName },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, ...yAxisName },
    series: [
      {
        type: chartType === 'line' ? 'line' : 'bar',
        smooth: chartType === 'line',
        data: rows.map((row) => Number(row[yField] ?? 0)),
        itemStyle: { color: '#0f6cbd', borderRadius: chartType === 'bar' ? [4, 4, 0, 0] : 0 },
      },
    ],
  };
}

function uniqueValues(values: string[]) {
  return Array.from(new Set(values));
}

function metricUnit(field: string): string {
  if (/charge|cost/i.test(field)) return 'USD';
  if (/length.*stay|stay.*day/i.test(field)) return 'days';
  if (/birth.*weight|weight/i.test(field)) return 'grams';
  if (/count|patient|record/i.test(field)) return 'records';
  return 'value';
}

function summarizeQueryScope(querySpec: Record<string, unknown> | null): string {
  if (!querySpec) return 'query scope unavailable';
  const parts: string[] = [];
  if (typeof querySpec.table === 'string') parts.push(querySpec.table);
  if (Array.isArray(querySpec.filters) && querySpec.filters.length) {
    const filters = querySpec.filters.slice(0, 3).map((filter) => {
      if (!filter || typeof filter !== 'object') return 'invalid filter';
      const item = filter as Record<string, unknown>;
      const field = typeof item.field === 'string' ? item.field : 'field';
      const operator = typeof item.op === 'string' ? item.op : '=';
      return `${field} ${operator} ${compactScopeValue(item.value)}`;
    });
    parts.push(filters.join(', '));
    if (querySpec.filters.length > 3) parts.push(`+${querySpec.filters.length - 3} filters`);
  }
  if (Array.isArray(querySpec.group_by) && querySpec.group_by.length) {
    parts.push(`grouped by ${querySpec.group_by.filter((item) => typeof item === 'string').join(', ')}`);
  }
  return parts.join(' · ').slice(0, 220) || 'structured query';
}

function compactScopeValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(compactScopeValue).join(', ')}]`.slice(0, 80);
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value).slice(0, 60);
  }
  return 'structured value';
}

function ResultTable({ result }: { result: QueryResult | null }) {
  if (!result) {
    return (
      <Box sx={{ p: 2 }}>
        <Typography color="text.secondary">Run a question or QuerySpec to see table results.</Typography>
      </Box>
    );
  }
  return (
    <TableContainer sx={{ minHeight: 0 }}>
      <Table stickyHeader size="small">
        <TableHead>
          <TableRow>
            {result.columns.map((column) => (
              <TableCell key={column}>{column}</TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {result.rows.map((row, index) => (
            <TableRow key={index}>
              {result.columns.map((column) => (
                <TableCell key={column} align={typeof row[column] === 'number' ? 'right' : 'left'}>
                  {formatCell(row[column])}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function formatCell(value: unknown) {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }
  if (value === null || value === undefined || value === '') {
    return '--';
  }
  return String(value);
}

function executionText(askResult: AskPayload | null, queryResult: QueryResult | null) {
  if (askResult) {
    return JSON.stringify(
      {
        intent: askResult.intent,
        tool_name: askResult.tool_name,
        analysis_goal: askResult.analysis_goal,
        execution_steps: askResult.execution_steps,
        chart_spec: askResult.chart_spec,
        compiled_sql: askResult.compiled_sql,
        compiled_params: askResult.compiled_params,
        metadata: askResult.result?.metadata,
      },
      null,
      2,
    );
  }
  if (queryResult) {
    return JSON.stringify({ metadata: queryResult.metadata }, null, 2);
  }
  return 'Ready.';
}
