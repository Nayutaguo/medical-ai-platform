-- Explicit reverse-order rollback for 002_identity_control_plane.up.sql.
-- Every deletion is idempotent, so repeated rollback is safe.

DROP TABLE IF EXISTS dataset_versions;
DROP TABLE IF EXISTS background_jobs;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS one_time_tokens;
DROP TABLE IF EXISTS auth_sessions;
DROP TABLE IF EXISTS role_permissions;
DROP TABLE IF EXISTS membership_roles;
DROP TABLE IF EXISTS permissions;
DROP TABLE IF EXISTS roles;
DROP TABLE IF EXISTS organization_memberships;
DROP TABLE IF EXISTS organizations;
DROP TABLE IF EXISTS users;
