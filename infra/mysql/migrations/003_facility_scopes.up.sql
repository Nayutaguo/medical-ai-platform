-- Canonical analytics facilities and fail-closed membership data scopes.
-- MySQL 8.0+; every DATETIME(6) value has UTC semantics and updated_at is
-- application-managed. Re-applying this file is safe.

CREATE TABLE IF NOT EXISTS facilities (
  id CHAR(36) NOT NULL,
  facility_key VARCHAR(128) NOT NULL COMMENT 'Matches analytics PermanentFacilityId',
  display_name VARCHAR(200) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  CONSTRAINT uq_facilities_facility_key UNIQUE (facility_key),
  CONSTRAINT ck_facilities_status CHECK (status IN ('active', 'disabled')),
  CONSTRAINT ck_facilities_version_positive CHECK (version >= 1),
  KEY ix_facilities_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS organization_facilities (
  organization_id CHAR(36) NOT NULL,
  facility_id CHAR(36) NOT NULL,
  granted_by_user_id CHAR(36) NULL,
  granted_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (organization_id, facility_id),
  CONSTRAINT ck_organization_facilities_version_positive CHECK (version >= 1),
  CONSTRAINT fk_organization_facilities_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE CASCADE,
  CONSTRAINT fk_organization_facilities_facility
    FOREIGN KEY (facility_id) REFERENCES facilities (id) ON DELETE CASCADE,
  CONSTRAINT fk_organization_facilities_granted_by_user
    FOREIGN KEY (granted_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_organization_facilities_facility (facility_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS membership_facility_scopes (
  membership_id CHAR(36) NOT NULL,
  facility_id CHAR(36) NOT NULL,
  organization_id CHAR(36) NOT NULL COMMENT 'Denormalized tenant key for composite foreign keys',
  granted_by_user_id CHAR(36) NULL,
  granted_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'UTC',
  updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)) COMMENT 'Application-managed UTC',
  version INT NOT NULL DEFAULT 1,
  PRIMARY KEY (membership_id, facility_id),
  CONSTRAINT ck_membership_facility_scopes_version_positive CHECK (version >= 1),
  CONSTRAINT fk_membership_facility_scopes_membership_org
    FOREIGN KEY (membership_id, organization_id)
    REFERENCES organization_memberships (id, organization_id) ON DELETE CASCADE,
  CONSTRAINT fk_membership_facility_scopes_org_facility
    FOREIGN KEY (organization_id, facility_id)
    REFERENCES organization_facilities (organization_id, facility_id) ON DELETE CASCADE,
  CONSTRAINT fk_membership_facility_scopes_granted_by_user
    FOREIGN KEY (granted_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
  KEY ix_membership_facility_scopes_membership_org (membership_id, organization_id),
  KEY ix_membership_facility_scopes_org_facility (organization_id, facility_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Explicit grants; no rows means no facility access, never unrestricted access';
