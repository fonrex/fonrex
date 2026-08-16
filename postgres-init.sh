#!/bin/bash
set -e

# This script will be executed during PostgreSQL container initialization
# It checks if the database exists and creates it if necessary

echo "🔍 Vérification de la base de données FonRex..."

# Checks if the fonrex database already exists
DATABASE_EXISTS=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT 1 FROM pg_database WHERE datname = 'fonrex'")

# If the database does not exist, create it
if [ -z "$DATABASE_EXISTS" ]; then
    echo "🛠️ Création de la base de données fonrex..."
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE fonrex WITH OWNER = fonrex ENCODING = 'UTF8';"
    echo "✅ Base de données fonrex créée avec succès!"
else
    echo "✅ Base de données fonrex existe déjà."
fi

# Ensure the user has the correct privileges
echo "🔑 Configuration des droits utilisateur..."
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "GRANT ALL PRIVILEGES ON DATABASE fonrex TO fonrex;"
echo "✅ Droits utilisateur configurés avec succès!"

echo "🎉 Initialisation PostgreSQL terminée!"
