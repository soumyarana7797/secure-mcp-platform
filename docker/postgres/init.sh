#!/bin/bash
# Runs once, on first start of an empty data directory.
# Roles are infrastructure (created here locally, by Terraform/RDS later); the schema is
# managed by Alembic migrations. The app role can never bypass row-level security.
set -euo pipefail
psql -v ON_ERROR_STOP=1 -v app_pw="$GATEWAY_APP_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE ROLE gateway_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
EOSQL
