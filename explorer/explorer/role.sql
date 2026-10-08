-- Run once as the database owner, with the password as a psql variable:
--   docker compose exec -T db psql -U x86db -d x86db -v pw="$EXPLORER_DB_PASSWORD" < explorer/explorer/role.sql
-- The explorer reads public and owns its own schema; it can write nowhere else.
CREATE ROLE explorer LOGIN PASSWORD :'pw' CONNECTION LIMIT 8;
ALTER ROLE explorer SET statement_timeout = '5s';
GRANT USAGE ON SCHEMA public TO explorer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO explorer;
-- Tables the controller creates later (a migration on a newer branch) are readable too.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO explorer;
CREATE SCHEMA explorer AUTHORIZATION explorer;
