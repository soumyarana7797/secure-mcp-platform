#!/bin/bash
# Runs once, on first start of an empty data directory.
# Roles are infrastructure (created here locally, by Terraform/RDS later); the schema is
# managed by Alembic migrations. None of these roles can bypass row-level security.
# For an EXISTING data volume, run `uv run python -m scripts.create_db_roles` instead.
set -euo pipefail
psql -v ON_ERROR_STOP=1 \
  -v app_pw="$GATEWAY_APP_PASSWORD" -v user_pw="$USER_SVC_PASSWORD" -v knowledge_pw="$KNOWLEDGE_SVC_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE gateway_app   LOGIN PASSWORD :'app_pw'       NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE user_svc      LOGIN PASSWORD :'user_pw'      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE knowledge_svc LOGIN PASSWORD :'knowledge_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
EOSQL
