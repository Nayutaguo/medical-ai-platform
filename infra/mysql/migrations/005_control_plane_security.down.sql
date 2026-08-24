-- Reverse only the 005 tenant-integrity and audit-provenance changes.
-- Historical organization/user snapshots remain in their original columns.

ALTER TABLE dataset_versions
  DROP FOREIGN KEY fk_dataset_versions_import_job_org,
  DROP INDEX ix_dataset_versions_import_job,
  ADD KEY ix_dataset_versions_import_job (import_job_id),
  ADD CONSTRAINT fk_dataset_versions_import_job
    FOREIGN KEY (import_job_id) REFERENCES background_jobs (id) ON DELETE SET NULL;

ALTER TABLE background_jobs
  DROP INDEX uq_background_jobs_id_organization;

ALTER TABLE organization_facilities
  ADD KEY ix_organization_facilities_facility (facility_id),
  DROP INDEX uq_organization_facilities_facility;

ALTER TABLE audit_events
  DROP CHECK ck_audit_events_actor_context,
  DROP CHECK ck_audit_events_actor_kind,
  DROP FOREIGN KEY fk_audit_events_actor_membership_context,
  DROP FOREIGN KEY fk_audit_events_organization,
  DROP INDEX ix_audit_events_membership_occurred,
  DROP INDEX ix_audit_events_actor_context,
  ADD CONSTRAINT fk_audit_events_organization
    FOREIGN KEY (organization_id) REFERENCES organizations (id) ON DELETE SET NULL,
  ADD CONSTRAINT fk_audit_events_actor_user
    FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE SET NULL,
  DROP COLUMN actor_membership_id,
  DROP COLUMN actor_kind;
