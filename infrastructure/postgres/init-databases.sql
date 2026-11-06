\getenv bootstrap_user POSTGRES_USER
\getenv airflow_user AIRFLOW_DB_USER
\getenv airflow_password AIRFLOW_DB_PASSWORD
\getenv warehouse_user WAREHOUSE_DB_USER
\getenv warehouse_password WAREHOUSE_DB_PASSWORD

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'airflow_user', :'airflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'airflow_user')
\gexec
ALTER ROLE :"airflow_user" WITH LOGIN PASSWORD :'airflow_password';

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'warehouse_user', :'warehouse_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'warehouse_user')
\gexec
ALTER ROLE :"warehouse_user" WITH LOGIN PASSWORD :'warehouse_password';

SELECT format('CREATE DATABASE airflow OWNER %I', :'airflow_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'airflow')
\gexec

ALTER DATABASE airflow OWNER TO :"airflow_user";
ALTER DATABASE food_intelligence OWNER TO :"warehouse_user";
REVOKE CONNECT ON DATABASE airflow FROM PUBLIC;
GRANT CONNECT ON DATABASE airflow TO :"airflow_user";
REVOKE CONNECT ON DATABASE food_intelligence FROM PUBLIC;
GRANT CONNECT ON DATABASE food_intelligence TO :"warehouse_user";

\connect airflow
ALTER SCHEMA public OWNER TO :"airflow_user";
SELECT format('ALTER TABLE %I.%I OWNER TO %I', schemaname, tablename, :'airflow_user')
FROM pg_tables
WHERE schemaname = 'public' AND tableowner = :'bootstrap_user'
\gexec
SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', n.nspname, c.relname, :'airflow_user')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.oid = c.relowner
WHERE n.nspname = 'public' AND c.relkind = 'S' AND r.rolname = :'bootstrap_user'
\gexec
SELECT format('ALTER VIEW %I.%I OWNER TO %I', schemaname, viewname, :'airflow_user')
FROM pg_views
WHERE schemaname = 'public' AND viewowner = :'bootstrap_user'
\gexec
SELECT format('ALTER MATERIALIZED VIEW %I.%I OWNER TO %I', schemaname, matviewname, :'airflow_user')
FROM pg_matviews
WHERE schemaname = 'public' AND matviewowner = :'bootstrap_user'
\gexec

\connect food_intelligence
ALTER SCHEMA public OWNER TO :"warehouse_user";
SELECT format('ALTER TABLE %I.%I OWNER TO %I', schemaname, tablename, :'warehouse_user')
FROM pg_tables
WHERE schemaname = 'public' AND tableowner = :'bootstrap_user'
\gexec
SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', n.nspname, c.relname, :'warehouse_user')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.oid = c.relowner
WHERE n.nspname = 'public' AND c.relkind = 'S' AND r.rolname = :'bootstrap_user'
\gexec
SELECT format('ALTER VIEW %I.%I OWNER TO %I', schemaname, viewname, :'warehouse_user')
FROM pg_views
WHERE schemaname = 'public' AND viewowner = :'bootstrap_user'
\gexec
SELECT format('ALTER MATERIALIZED VIEW %I.%I OWNER TO %I', schemaname, matviewname, :'warehouse_user')
FROM pg_matviews
WHERE schemaname = 'public' AND matviewowner = :'bootstrap_user'
\gexec
