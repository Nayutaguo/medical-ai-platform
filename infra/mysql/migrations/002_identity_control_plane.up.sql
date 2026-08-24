-- Identity, authorization, audit, background-job, and dataset-version control plane.
-- MySQL 8.0+; every DATETIME(6) value has UTC semantics.
-- Re-applying this file is safe because every table and inline index is created
-- only as part of CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS users (
  id CHAR(36) NOT NULL,
  email VARCHAR(320) NOT NULL,
  email_normalized VARCHAR(320) NOT NULL,
  password_hash VARCHAR(255) NULL COMMENT 'Password verifier; never plaintext',
  password_algorithm VARCHAR(32) NULL,
  display_name VARCHAR(120) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  auth_version INT NOT NULL DEFAULT 1,
  email_verified_at DATETIME(6) NULL COMMENT 'UTC',
  last_login_at DATETIME(6) NULL COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  deleted_at DATETIME(6) NULL COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_users_email_normalized UNIQUE (email_normalized),
  CONSTRAINT ck_users_status CHECK (status IN ('invited', 'active', 'suspended', 'disabled')),
  CONSTRAINT ck_users_auth_version_positive CHECK (auth_version >= 1),
  CONSTRAINT ck_users_version_positive CHECK (version >= 1),
  KEY ix_users_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS organizations (
  id CHAR(36) NOT NULL,
  name VARCHAR(200) NOT NULL,
  slug VARCHAR(100) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  deleted_at DATETIME(6) NULL COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_organizations_slug UNIQUE (slug),
  CONSTRAINT ck_organizations_status CHECK (status IN ('active', 'suspended', 'disabled')),
  CONSTRAINT ck_organizations_version_positive CHECK (version >= 1),
  KEY ix_organizations_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS organization_memberships (
  id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  authorization_version INT NOT NULL DEFAULT 1,
  joined_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_organization_memberships_organization_user UNIQUE (organization_id, user_id),
  CONSTRAINT uq_organization_memberships_id_organization UNIQUE (id, organization_id),
  CONSTRAINT uq_organization_memberships_session_context UNIQUE (id, user_id, organization_id),
  CONSTRAINT ck_organization_memberships_status CHECK (status IN ('invited', 'active', 'suspended', 'removed')),
  CONSTRAINT ck_organization_memberships_authorization_version_positive CHECK (authorization_version >= 1),
  CONSTRAINT ck_organization_memberships_version_positive CHECK (version >= 1),
  CONSTRAINT fk_organization_memberships_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE CASCADE,
  CONSTRAINT fk_organization_memberships_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  KEY ix_organization_memberships_user_status (user_id, status),
  KEY ix_organization_memberships_organization_status (organization_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS roles (
  id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  role_key VARCHAR(100) NOT NULL,
  name VARCHAR(120) NOT NULL,
  description TEXT NULL,
  is_system BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_roles_organization_role_key UNIQUE (organization_id, role_key),
  CONSTRAINT uq_roles_id_organization UNIQUE (id, organization_id),
  CONSTRAINT ck_roles_version_positive CHECK (version >= 1),
  CONSTRAINT fk_roles_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE CASCADE,
  KEY ix_roles_organization_system (organization_id, is_system)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS permissions (
  id CHAR(36) NOT NULL,
  permission_key VARCHAR(160) NOT NULL,
  resource VARCHAR(80) NOT NULL,
  action VARCHAR(80) NOT NULL,
  description TEXT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_permissions_permission_key UNIQUE (permission_key),
  CONSTRAINT uq_permissions_resource_action UNIQUE (resource, action),
  CONSTRAINT ck_permissions_version_positive CHECK (version >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS membership_roles (
  organization_id CHAR(36) NOT NULL,
  membership_id CHAR(36) NOT NULL,
  role_id CHAR(36) NOT NULL,
  assigned_by_user_id CHAR(36) NULL,
  assigned_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (membership_id, role_id),
  CONSTRAINT ck_membership_roles_version_positive CHECK (version >= 1),
  CONSTRAINT fk_membership_roles_membership_organization
    FOREIGN KEY (membership_id, organization_id)
    REFERENCES organization_memberships (id, organization_id) ON DELETE CASCADE,
  CONSTRAINT fk_membership_roles_role_organization
    FOREIGN KEY (role_id, organization_id)
    REFERENCES roles (id, organization_id) ON DELETE CASCADE,
  CONSTRAINT fk_membership_roles_assigned_by_user
    FOREIGN KEY (assigned_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_membership_roles_organization_role (organization_id, role_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id CHAR(36) NOT NULL,
  permission_id CHAR(36) NOT NULL,
  granted_by_user_id CHAR(36) NULL,
  granted_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (role_id, permission_id),
  CONSTRAINT ck_role_permissions_version_positive CHECK (version >= 1),
  CONSTRAINT fk_role_permissions_role
    FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE,
  CONSTRAINT fk_role_permissions_permission
    FOREIGN KEY (permission_id) REFERENCES permissions (id) ON DELETE CASCADE,
  CONSTRAINT fk_role_permissions_granted_by_user
    FOREIGN KEY (granted_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_role_permissions_permission (permission_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS auth_sessions (
  id CHAR(36) NOT NULL COMMENT 'Non-secret session identifier',
  membership_id CHAR(36) NOT NULL,
  user_id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  session_token_hash BINARY(32) NOT NULL COMMENT 'SHA-256 or stronger digest; never plaintext',
  refresh_token_hash BINARY(32) NULL COMMENT 'SHA-256 or stronger digest; never plaintext',
  csrf_token_hash BINARY(32) NOT NULL COMMENT 'SHA-256 or stronger digest; never plaintext',
  ip_address_hash BINARY(32) NULL,
  user_agent_hash BINARY(32) NULL,
  expires_at DATETIME(6) NOT NULL COMMENT 'UTC',
  idle_expires_at DATETIME(6) NOT NULL COMMENT 'UTC',
  last_seen_at DATETIME(6) NULL COMMENT 'UTC',
  revoked_at DATETIME(6) NULL COMMENT 'UTC',
  identity_version INT NOT NULL,
  authorization_version INT NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_auth_sessions_session_token_hash UNIQUE (session_token_hash),
  CONSTRAINT uq_auth_sessions_refresh_token_hash UNIQUE (refresh_token_hash),
  CONSTRAINT uq_auth_sessions_csrf_token_hash UNIQUE (csrf_token_hash),
  CONSTRAINT ck_auth_sessions_identity_version_positive CHECK (identity_version >= 1),
  CONSTRAINT ck_auth_sessions_authorization_version_positive CHECK (authorization_version >= 1),
  CONSTRAINT ck_auth_sessions_idle_before_absolute_expiry CHECK (idle_expires_at <= expires_at),
  CONSTRAINT ck_auth_sessions_version_positive CHECK (version >= 1),
  CONSTRAINT fk_auth_sessions_membership_context
    FOREIGN KEY (membership_id, user_id, organization_id)
    REFERENCES organization_memberships (id, user_id, organization_id) ON DELETE CASCADE,
  KEY ix_auth_sessions_membership_expires (membership_id, expires_at),
  KEY ix_auth_sessions_user_expires (user_id, expires_at),
  KEY ix_auth_sessions_organization_expires (organization_id, expires_at),
  KEY ix_auth_sessions_revoked_expires (revoked_at, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS one_time_tokens (
  id CHAR(36) NOT NULL COMMENT 'Non-secret token record identifier',
  user_id CHAR(36) NOT NULL,
  purpose VARCHAR(48) NOT NULL,
  token_hash BINARY(32) NOT NULL COMMENT 'SHA-256 or stronger digest; never plaintext',
  expires_at DATETIME(6) NOT NULL COMMENT 'UTC',
  consumed_at DATETIME(6) NULL COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_one_time_tokens_token_hash UNIQUE (token_hash),
  CONSTRAINT ck_one_time_tokens_version_positive CHECK (version >= 1),
  CONSTRAINT fk_one_time_tokens_user
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  KEY ix_one_time_tokens_user_purpose_expires (user_id, purpose, expires_at),
  KEY ix_one_time_tokens_expires_consumed (expires_at, consumed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS audit_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  occurred_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  request_id VARCHAR(128) NULL,
  organization_id CHAR(36) NULL,
  actor_user_id CHAR(36) NULL,
  action VARCHAR(120) NOT NULL,
  resource_type VARCHAR(80) NOT NULL,
  resource_id VARCHAR(128) NULL,
  outcome VARCHAR(32) NOT NULL,
  error_code VARCHAR(80) NULL,
  details JSON NULL COMMENT 'Redacted aggregate metadata only',
  ip_address_hash BINARY(32) NULL,
  user_agent_hash BINARY(32) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('success', 'denied', 'failure')),
  CONSTRAINT ck_audit_events_version_positive CHECK (version >= 1),
  CONSTRAINT fk_audit_events_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE SET NULL,
  CONSTRAINT fk_audit_events_actor_user
    FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_audit_events_request (request_id),
  KEY ix_audit_events_organization_occurred (organization_id, occurred_at),
  KEY ix_audit_events_actor_occurred (actor_user_id, occurred_at),
  KEY ix_audit_events_resource (resource_type, resource_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS background_jobs (
  id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  requested_by_user_id CHAR(36) NULL,
  job_type VARCHAR(80) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'queued',
  idempotency_key VARCHAR(128) NULL,
  input_payload JSON NULL COMMENT 'Validated and redacted input only',
  result_payload JSON NULL COMMENT 'Aggregate metadata or artifact references only',
  error_code VARCHAR(80) NULL,
  error_message VARCHAR(500) NULL,
  progress_percent SMALLINT NOT NULL DEFAULT 0,
  attempt_count SMALLINT NOT NULL DEFAULT 0,
  max_attempts SMALLINT NOT NULL DEFAULT 3,
  lease_token_hash BINARY(32) NULL COMMENT 'Worker lease digest; never plaintext',
  queued_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  started_at DATETIME(6) NULL COMMENT 'UTC',
  heartbeat_at DATETIME(6) NULL COMMENT 'UTC',
  completed_at DATETIME(6) NULL COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_background_jobs_organization_idempotency UNIQUE (organization_id, idempotency_key),
  CONSTRAINT ck_background_jobs_status
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  CONSTRAINT ck_background_jobs_progress CHECK (progress_percent BETWEEN 0 AND 100),
  CONSTRAINT ck_background_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1),
  CONSTRAINT ck_background_jobs_version_positive CHECK (version >= 1),
  CONSTRAINT fk_background_jobs_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE RESTRICT,
  CONSTRAINT fk_background_jobs_requested_by_user
    FOREIGN KEY (requested_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_background_jobs_status_queued (status, queued_at),
  KEY ix_background_jobs_organization_status (organization_id, status, queued_at),
  KEY ix_background_jobs_requested_by (requested_by_user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS dataset_versions (
  id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL,
  dataset_key VARCHAR(120) NOT NULL,
  version_number INT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'staged',
  source_file_name VARCHAR(512) NOT NULL,
  source_sha256 CHAR(64) NOT NULL,
  schema_version VARCHAR(64) NOT NULL,
  row_count BIGINT NOT NULL DEFAULT 0,
  rejected_row_count BIGINT NOT NULL DEFAULT 0,
  quality_profile JSON NULL,
  imported_by_user_id CHAR(36) NULL,
  import_job_id CHAR(36) NULL,
  activated_at DATETIME(6) NULL COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_dataset_versions_organization_dataset_version
    UNIQUE (organization_id, dataset_key, version_number),
  CONSTRAINT uq_dataset_versions_organization_dataset_source
    UNIQUE (organization_id, dataset_key, source_sha256),
  CONSTRAINT ck_dataset_versions_number_positive CHECK (version_number >= 1),
  CONSTRAINT ck_dataset_versions_row_counts CHECK (row_count >= 0 AND rejected_row_count >= 0),
  CONSTRAINT ck_dataset_versions_status CHECK (status IN ('staged', 'active', 'superseded', 'failed')),
  CONSTRAINT ck_dataset_versions_version_positive CHECK (version >= 1),
  CONSTRAINT fk_dataset_versions_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE RESTRICT,
  CONSTRAINT fk_dataset_versions_imported_by_user
    FOREIGN KEY (imported_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  CONSTRAINT fk_dataset_versions_import_job
    FOREIGN KEY (import_job_id) REFERENCES background_jobs (id) ON DELETE SET NULL,
  KEY ix_dataset_versions_organization_status (organization_id, dataset_key, status),
  KEY ix_dataset_versions_import_job (import_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
