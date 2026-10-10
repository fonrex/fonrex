#!/bin/bash
set -e

# This script will be executed during PostgreSQL container initialization
# It checks if the database exists and creates it if necessary

echo "🔍 Checking the Fonrex database..."

# Checks if the fonrex database already exists
DATABASE_EXISTS=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT 1 FROM pg_database WHERE datname = 'fonrex'")

# If the database does not exist, create it
if [ -z "$DATABASE_EXISTS" ]; then
    echo "🛠️ Creating the fonrex database..."
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE fonrex WITH OWNER = fonrex ENCODING = 'UTF8';"
    echo "✅ fonrex database created!"
else
    echo "✅ The fonrex database already exists."
fi

# Ensure the user has the correct privileges
echo "🔑 Setting the user privileges..."
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "GRANT ALL PRIVILEGES ON DATABASE fonrex TO fonrex;"
echo "✅ User privileges set!"

echo "🎉 PostgreSQL initialisation done!"
