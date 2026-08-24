-- Explicit reverse-order rollback for 003_facility_scopes.up.sql.
-- Repeated rollback is safe.

DROP TABLE IF EXISTS membership_facility_scopes;
DROP TABLE IF EXISTS organization_facilities;
DROP TABLE IF EXISTS facilities;
