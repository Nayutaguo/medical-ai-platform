-- Reconcile tenant integrity and immutable audit actor provenance.
-- MySQL 8.0+. Online Alembic execution performs integrity preflight checks
-- before running equivalent conditional DDL; this file is also the reviewed
-- deterministic SQL used for fresh/offline upgrade generation.

ALTER TABLE audit_events
  ADD COLUMN actor_kind VARCHAR(16) NULL
    COMMENT 'Actor provenance: a trusted membership user or a system process.'
    AFTER organization_id,
  ADD COLUMN actor_membership_id CHAR(36) NULL
    COMMENT 'Trusted organization membership for user actors; NULL for system actors.'
    AFTER actor_kind;

UPDATE audit_events AS ae
INNER JOIN organization_memberships AS om
  ON om.user_id = ae.actor_user_id
 AND om.organization_id = ae.organization_id
SET ae.actor_kind = 'user',
    ae.actor_membership_id = om.id
WHERE ae.actor_user_id IS NOT NULL;

UPDATE audit_events
SET actor_kind = 'system',
    actor_membership_id = NULL
WHERE actor_user_id IS NULL;

ALTER TABLE audit_events
  DROP FOREIGN KEY fk_audit_events_actor_user,
  DROP FOREIGN KEY fk_audit_events_organization;

ALTER TABLE audit_events
  MODIFY COLUMN actor_kind VARCHAR(16) NOT NULL
    COMMENT 'Actor provenance: a trusted membership user or a system process.',
  ADD CONSTRAINT fk_audit_events_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE RESTRICT,
  ADD CONSTRAINT fk_audit_events_actor_membership_context
    FOREIGN KEY (actor_membership_id, actor_user_id, organization_id)
    REFERENCES organization_memberships (id, user_id, organization_id)
    ON DELETE RESTRICT,
  ADD CONSTRAINT ck_audit_events_actor_kind
    CHECK (actor_kind IN ('user', 'system')),
  ADD CONSTRAINT ck_audit_events_actor_context
    CHECK (
      (actor_kind = 'user'
       AND actor_membership_id IS NOT NULL
       AND actor_user_id IS NOT NULL
       AND organization_id IS NOT NULL)
      OR
      (actor_kind = 'system'
       AND actor_membership_id IS NULL
       AND actor_user_id IS NULL)
    ),
  ADD KEY ix_audit_events_actor_context
    (actor_membership_id, actor_user_id, organization_id),
  ADD KEY ix_audit_events_membership_occurred
    (actor_membership_id, occurred_at);

ALTER TABLE background_jobs
  ADD CONSTRAINT uq_background_jobs_id_organization UNIQUE (id, organization_id);

ALTER TABLE organization_facilities
  ADD CONSTRAINT uq_organization_facilities_facility UNIQUE (facility_id),
  DROP INDEX ix_organization_facilities_facility;

ALTER TABLE dataset_versions
  DROP FOREIGN KEY fk_dataset_versions_import_job,
  DROP INDEX ix_dataset_versions_import_job,
  ADD KEY ix_dataset_versions_import_job (import_job_id, organization_id),
  ADD CONSTRAINT fk_dataset_versions_import_job_org
    FOREIGN KEY (import_job_id, organization_id)
    REFERENCES background_jobs (id, organization_id)
    ON DELETE RESTRICT;
