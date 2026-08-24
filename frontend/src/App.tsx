import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactElement, ReactNode } from 'react';
import * as echarts from 'echarts';
import {
  Alert,
  AppBar,
  Box,
  Button,
  Chip,
  CircularProgress,
  CssBaseline,
  Divider,
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
import {
  ApiError,
  ask,
  getCurrentSession,
  getDistinct,
  getHealth,
  getSchema,
  isAuthenticationDisabled,
  login,
  logout,
  registerAccount,
  runQuerySpec,
} from './api';
import { theme } from './theme';
import type {
  AgentInsight,
  AgentIntent,
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

export function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus>('checking');
  const [session, setSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authFieldErrors, setAuthFieldErrors] = useState<AuthFieldError[]>([]);
  const [authNotice, setAuthNotice] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [schema, setSchema] = useState<SchemaPayload | null>(null);
  const [distinct, setDistinct] = useState<DistinctPayload | null>(null);
  const [question, setQuestion] = useState(defaultQuestion);
  const [querySpecText, setQuerySpecText] = useState(JSON.stringify(sampleSpec, null, 2));
  const [askResult, setAskResult] = useState<AskPayload | null>(null);
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const canAccessWorkbench =
    authStatus === 'anonymous' ||
    (authStatus === 'authenticated' && session?.permissions.includes('analytics.schema.read') === true);

  const reportRequestFailure = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) {
      setSession(null);
      setAuthStatus('unauthenticated');
    }
    setError(err instanceof Error ? err.message : String(err));
  }, []);

  const loadBasics = useCallback(async () => {
    setError(null);
    try {
      const [nextHealth, nextSchema] = await Promise.all([getHealth(), getSchema()]);
      setHealth(nextHealth);
      setSchema(nextSchema);
    } catch (err) {
      reportRequestFailure(err);
    }
  }, [reportRequestFailure]);

  const restoreAuthentication = useCallback(async () => {
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
    setAuthStatus('checking');
    try {
      const activeSession = await getCurrentSession();
      setSession(activeSession);
      setAuthStatus('authenticated');
    } catch (err) {
      setSession(null);
      if (isAuthenticationDisabled(err)) {
        setAuthStatus('anonymous');
        return;
      }
      if (err instanceof ApiError && err.status === 401) {
        setAuthStatus('unauthenticated');
        return;
      }
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
      setAuthStatus('error');
    }
  }, []);

  useEffect(() => {
    void restoreAuthentication();
  }, [restoreAuthentication]);

  useEffect(() => {
    if (canAccessWorkbench) {
      void loadBasics();
    }
  }, [canAccessWorkbench, loadBasics]);

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
      setSession(activeSession);
      setAuthStatus('authenticated');
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleRegistration = async (credentials: RegistrationCredentials): Promise<boolean> => {
    setAuthLoading(true);
    setAuthError(null);
    setAuthFieldErrors([]);
    setAuthNotice(null);
    setError(null);
    try {
      await registerAccount(credentials);
      setAuthNotice('注册成功，请使用新账号登录。数据权限需要由组织管理员另行分配。');
      return true;
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : String(err));
      setAuthFieldErrors(err instanceof ApiError ? err.fieldErrors : []);
      return false;
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
    setError(null);
    try {
      await logout();
      setSession(null);
      setHealth(null);
      setSchema(null);
      setDistinct(null);
      setAskResult(null);
      setQueryResult(null);
      setAuthStatus('unauthenticated');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAuthLoading(false);
    }
  };

  const handleAsk = async () => {
    setLoading(true);
    setError(null);
    setAskResult(null);
    setQueryResult(null);
    try {
      const payload = await ask(question);
      setAskResult(payload);
      if (payload.query_spec) {
        setQuerySpecText(JSON.stringify(payload.query_spec, null, 2));
      }
      setQueryResult(payload.result ?? null);
    } catch (err) {
      reportRequestFailure(err);
    } finally {
      setLoading(false);
    }
  };

  const handleRunSpec = async () => {
    setLoading(true);
    setError(null);
    setAskResult(null);
    setQueryResult(null);
    try {
      const parsed = JSON.parse(querySpecText) as Record<string, unknown>;
      const payload = await runQuerySpec(parsed);
      setAskResult(null);
      setQueryResult(payload);
    } catch (err) {
      reportRequestFailure(err);
    } finally {
      setLoading(false);
    }
  };

  const handleDistinct = async (field: string) => {
    setError(null);
    try {
      setDistinct(await getDistinct(field));
    } catch (err) {
      reportRequestFailure(err);
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
                    <Chip size="small" label={schema ? `${columns.length} fields` : 'Schema --'} />
                  </>
                ) : null}
                {session ? (
                  <>
                    <Chip
                      size="small"
                      variant="outlined"
                      label={`${session.user.display_name || session.user.email} · ${session.organization.name}`}
                    />
                    <Button size="small" color="inherit" onClick={() => void handleLogout()} disabled={authLoading}>
                      退出
                    </Button>
                  </>
                ) : (
                  <Chip size="small" variant="outlined" label="匿名开发模式" />
                )}
                <Tooltip title="Refresh">
                  <IconButton size="small" onClick={() => void loadBasics()}>
                    <RefreshIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            ) : null}
          </Toolbar>
        </AppBar>

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
            <SchemaPanel columns={columns} distinct={distinct} onDistinct={handleDistinct} />

            <Stack spacing={1.5} minWidth={0} minHeight={0}>
              <Paper sx={{ p: 1.5 }}>
                <SectionTitle icon={<SmartToyIcon />} title="Ask" secondary="Natural language -> QuerySpec -> MySQL" />
                <TextField
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  multiline
                  minRows={4}
                  fullWidth
                  sx={{ mt: 1.5 }}
                />
                <Stack direction="row" flexWrap="wrap" gap={0.75} sx={{ mt: 1.25 }}>
                  {sampleQuestions.map((sample) => (
                    <Chip key={sample} size="small" label={sample} onClick={() => setQuestion(sample)} />
                  ))}
                </Stack>
                <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
                  <Button startIcon={loading ? <CircularProgress size={16} color="inherit" /> : <PlayArrowIcon />} variant="contained" onClick={handleAsk} disabled={loading}>
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
                          ? `${queryResult.row_count} rows in ${queryResult.query_time_ms} ms`
                          : 'Waiting for query'
                    }
                  />
                </Box>
                <InsightPanel insight={askResult?.insight ?? null} intent={askResult?.intent ?? null} />
                <ResultChart result={queryResult} chartSpec={askResult?.chart_spec ?? null} />
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
                      <IconButton size="small" onClick={handleRunSpec} disabled={loading}>
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
  onRegister: (credentials: RegistrationCredentials) => Promise<boolean>;
  onModeChange: () => void;
}) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [organizationId, setOrganizationId] = useState('');
  const [invitationToken, setInvitationToken] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [passwordConfirmation, setPasswordConfirmation] = useState('');
  const [clientError, setClientError] = useState<string | null>(null);

  const changeMode = (_event: React.SyntheticEvent, nextMode: 'login' | 'register') => {
    setMode(nextMode);
    setPassword('');
    setPasswordConfirmation('');
    setInvitationToken('');
    setClientError(null);
    onModeChange();
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setClientError(null);
    if (mode === 'login') {
      if (!email.trim() || !password) return;
      await onLogin(email.trim(), password, organizationId);
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
    const registered = await onRegister({
      invitation_token: invitationToken,
      email,
      display_name: displayName,
      password,
    });
    if (registered) {
      setMode('login');
      setInvitationToken('');
      setDisplayName('');
      setPassword('');
      setPasswordConfirmation('');
      setOrganizationId('');
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
            <Typography variant="h2">访问工作台</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              使用组织账号登录，或通过管理员发放的一次性邀请码注册。
            </Typography>
          </Box>
          <Tabs value={mode} onChange={changeMode} variant="fullWidth" aria-label="登录或注册">
            <Tab value="login" label="登录" disabled={loading} />
            <Tab value="register" label="注册" disabled={loading} />
          </Tabs>
          {notice ? <Alert severity="success">{notice}</Alert> : null}
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
            autoFocus={mode === 'login'}
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
            />
          ) : null}
          <TextField
            label="密码"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            inputProps={mode === 'register' ? { minLength: 12 } : undefined}
            required
            fullWidth
            helperText={mode === 'register' ? '至少 12 个字符；最终规则以服务端校验为准。' : undefined}
          />
          {mode === 'register' ? (
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
              <Alert severity="info" icon={false}>
                注册仅创建组织成员账号，不会自动授予医疗数据查询或管理权限。
              </Alert>
            </>
          ) : (
            <TextField
              label="组织 ID（可选）"
              value={organizationId}
              onChange={(event) => setOrganizationId(event.target.value)}
              autoComplete="off"
              fullWidth
            />
          )}
          <Button
            type="submit"
            variant="contained"
            disabled={
              loading ||
              (mode === 'login' ? !email.trim() || !password : !registerFormComplete)
            }
          >
            {loading ? <CircularProgress size={20} color="inherit" /> : mode === 'login' ? '登录' : '注册账号'}
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

function SchemaPanel({ columns, distinct, onDistinct }: { columns: SchemaColumn[]; distinct: DistinctPayload | null; onDistinct: (field: string) => void }) {
  return (
    <Paper sx={{ minHeight: { xs: 360, lg: 0 }, display: 'flex', flexDirection: 'column' }}>
      <Box sx={{ p: 1.5 }}>
        <SectionTitle icon={<SchemaIcon />} title="Schema" secondary={`${columns.length} allowlisted fields`} />
        <Stack direction="row" flexWrap="wrap" gap={0.75} sx={{ mt: 1.5 }}>
          {['AgeGroup', 'Gender', 'AdmissionType', 'PaymentTypology1', 'CCSRDiagnosisDescription'].map((field) => (
            <Chip key={field} size="small" label={field} onClick={() => onDistinct(field)} />
          ))}
        </Stack>
        <Box sx={{ mt: 1.5, p: 1, minHeight: 66, maxHeight: 120, overflow: 'auto', border: '1px solid rgba(16,24,40,0.12)', borderRadius: 1, bgcolor: '#fcfcfd' }}>
          <Typography variant="caption" color="text.secondary">
            {distinct ? `${distinct.field}: ${distinct.values.join(', ')}${distinct.truncated ? ' ...' : ''}` : 'Click a chip to inspect distinct values.'}
          </Typography>
        </Box>
      </Box>
      <Divider />
      <List dense sx={{ overflow: 'auto', flex: 1, py: 0 }}>
        {columns.map((column) => (
          <ListItemButton key={column.name} onClick={() => onDistinct(column.name)} sx={{ alignItems: 'flex-start', borderBottom: '1px solid rgba(16,24,40,0.08)' }}>
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

function ResultChart({ result, chartSpec }: { result: QueryResult | null; chartSpec: ChartSpec | null }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartColumns = result?.columns ?? [];
  const rows = useMemo(() => result?.rows ?? [], [result]);

  useEffect(() => {
    if (!ref.current) return;
    const instance = echarts.init(ref.current);
    const fields = resolveChartFields(result, chartSpec);
    const { xField, yField } = fields;
    if (!result || !xField || !yField || rows.length === 0 || fields.chartType === 'table') {
      instance.setOption({
        title: { text: 'No chartable result yet', left: 'center', top: 'middle', textStyle: { color: '#667085', fontSize: 13, fontWeight: 400 } },
      });
    } else {
      instance.setOption(buildChartOption(rows, { ...fields, xField, yField }, chartSpec), true);
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
  }, [chartColumns, chartSpec, result, rows]);

  return <Box ref={ref} sx={{ minHeight: 0, borderBottom: '1px solid rgba(16,24,40,0.12)' }} />;
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
): echarts.EChartsOption {
  const { chartType, xField, yField, seriesField } = fields;
  const title = chartSpec?.title ?? yField;

  if (chartType === 'number') {
    return {
      title: { text: title, left: 'center', top: 20, textStyle: { color: '#344054', fontSize: 14 } },
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
      title: { text: title, left: 16, top: 8, textStyle: { color: '#344054', fontSize: 13 } },
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
      title: { text: title, left: 16, top: 8, textStyle: { color: '#344054', fontSize: 13 } },
      grid: { left: 64, right: 24, top: 56, bottom: 42 },
      tooltip: { trigger: 'axis' },
      legend: { top: 28, textStyle: { color: '#667085' } },
      xAxis: { type: 'category', data: xValues, axisLabel: { color: '#344054' } },
      yAxis: { type: 'value', axisLabel: { color: '#667085' } },
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
    title: { text: title, left: 16, top: 8, textStyle: { color: '#344054', fontSize: 13 } },
    grid: { left: 68, right: 24, top: 52, bottom: 42 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: rows.map((row) => String(row[xField])), axisLabel: { color: '#344054' } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' } },
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
