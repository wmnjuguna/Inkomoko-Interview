#!/bin/bash
# Runs once, on an empty data volume. Creates one database and one login per service.
# Fineract migrates its own schemas on boot; this script only creates the empty databases.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE USER ${FINERACT_DB_USER} WITH PASSWORD '${FINERACT_DB_PASSWORD}';
  CREATE DATABASE fineract_tenants OWNER ${FINERACT_DB_USER};
  CREATE DATABASE fineract_default OWNER ${FINERACT_DB_USER};

  CREATE USER ${INTEGRATION_DB_USER} WITH PASSWORD '${INTEGRATION_DB_PASSWORD}';
  CREATE DATABASE integration_db OWNER ${INTEGRATION_DB_USER};
  -- Empty database for the Integration Service tests; they rebuild its schema on each run
  CREATE DATABASE integration_test OWNER ${INTEGRATION_DB_USER};

  CREATE USER ${ERP_DB_USER} WITH PASSWORD '${ERP_DB_PASSWORD}';
  CREATE DATABASE erp_db OWNER ${ERP_DB_USER};
  -- Empty database for the Mock ERP integration tests; they rebuild its schema on each run
  CREATE DATABASE erp_test OWNER ${ERP_DB_USER};
EOSQL
