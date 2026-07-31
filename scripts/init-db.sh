#!/bin/bash
# Create plugin daemon database on first boot
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
  DO $$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dify_plugin') THEN
      CREATE DATABASE dify_plugin;
      GRANT ALL PRIVILEGES ON DATABASE dify_plugin TO dify;
    END IF;
  END
  $$;
EOSQL
