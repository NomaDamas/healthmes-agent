#!/bin/bash
# Creates the dedicated `healthmes` database (own role) next to the
# open-wearables database inside the shared postgres container.
#
# Mounted at /docker-entrypoint-initdb.d by the root docker-compose.yml, so it
# runs exactly once, on first initialization of the postgres data volume.
# POSTGRES_USER / POSTGRES_DB come from the container environment; the
# HEALTHMES_DB_* variables are set in docker-compose.yml (defaults below).
set -euo pipefail

: "${HEALTHMES_DB_NAME:=healthmes}"
: "${HEALTHMES_DB_USER:=healthmes}"
: "${HEALTHMES_DB_PASSWORD:=healthmes}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE ROLE "${HEALTHMES_DB_USER}" WITH LOGIN PASSWORD '${HEALTHMES_DB_PASSWORD}';
	CREATE DATABASE "${HEALTHMES_DB_NAME}" OWNER "${HEALTHMES_DB_USER}";
	GRANT ALL PRIVILEGES ON DATABASE "${HEALTHMES_DB_NAME}" TO "${HEALTHMES_DB_USER}";
EOSQL

echo "healthmes database '${HEALTHMES_DB_NAME}' created (owner: ${HEALTHMES_DB_USER})"
