export interface HealthPayload {
  status: 'alive';
}

export interface AuthSession {
  user: {
    id: string;
    email: string;
    display_name: string;
  };
  organization: {
    id: string;
    name: string;
    membership_id: string;
  };
  permissions: string[];
  expires_at: string;
  idle_expires_at: string;
}

export interface LoginCredentials {
  email: string;
  password: string;
  organization_id?: string;
}

export interface LoginPayload {
  session: AuthSession;
  csrf_token: string;
  csrf_cookie_name: string;
  csrf_header_name: string;
}

export interface RegistrationCredentials {
  invitation_token: string;
  email: string;
  display_name: string;
  password: string;
}

export interface RegistrationPayload {
  registered: true;
  organization_id: string;
}

export interface CurrentSessionPayload {
  session: AuthSession;
  csrf_cookie_name: string;
  csrf_header_name: string;
}

export interface LogoutPayload {
  revoked: boolean;
}

export interface AdminRole {
  id: string;
  role_key: string;
  name: string;
  description: string | null;
  permissions: string[];
  version: number;
}

export interface AdminFacility {
  id: string;
  facility_key: string;
  display_name: string | null;
  status: 'active' | 'disabled';
  version: number;
}

export interface AdminMember {
  membership_id: string;
  user_id: string;
  email: string;
  display_name: string;
  user_status: 'invited' | 'active' | 'suspended' | 'disabled';
  membership_status: 'invited' | 'active' | 'suspended' | 'removed';
  authorization_version: number;
  version: number;
  roles: AdminRole[];
  facilities: AdminFacility[];
}

export interface AdminListPayload<T> {
  items: T[];
  next_cursor: string | null;
}

export interface IssuedInvitation {
  membership_id: string;
  token: string;
  expires_at: string;
}

export interface FacilitySyncPayload {
  created_count: number;
  existing_count: number;
}

export interface SchemaColumn {
  name: string;
  type: string;
  description: string;
  allowed_operations: string[];
  numeric: boolean;
}

export interface SchemaPayload {
  tables: Array<{
    name: string;
    description: string;
    columns: SchemaColumn[];
  }>;
}

export interface QueryResult {
  columns: string[];
  rows: Record<string, string | number | boolean | null>[];
  row_count: number;
  query_time_ms: number;
  truncated: boolean;
  metadata: Record<string, unknown>;
}

export interface AgentIntent {
  intent_type: string;
  route: string;
  confidence: number;
  reason: string;
}

export interface ChartSpec {
  chart_type: 'table' | 'bar' | 'grouped_bar' | 'line' | 'pie' | 'number';
  x_field?: string | null;
  y_field?: string | null;
  series_field?: string | null;
  title: string;
  reason: string;
}

export interface AgentInsight {
  summary: string;
  chart_reading: string;
  observations: string[];
  limitations: string[];
  follow_up_questions: string[];
}

export interface AskPayload {
  question: string;
  intent?: AgentIntent;
  tool_name?: string;
  tool_args?: Record<string, unknown>;
  analysis_goal?: string;
  assumptions?: string[];
  execution_steps?: string[];
  query_spec: Record<string, unknown> | null;
  chart_spec?: ChartSpec | null;
  compiled_sql: string | null;
  compiled_params: Record<string, unknown>;
  result?: QueryResult;
  tool_result?: Record<string, unknown>;
  insight?: AgentInsight | null;
  warnings?: Array<{
    code: string;
    message: string;
  }>;
  disclaimer?: string;
}

export interface DistinctPayload {
  table: string;
  field: string;
  values: Array<string | number | null>;
  row_count: number;
  truncated: boolean;
  query_time_ms: number;
}
