#!/bin/bash
set -e

echo "Starting FonRex API..."
export HOME=/tmp

# Function to check if a service is ready
check_service() {
    local host=$1
    local port=$2
    local service_name=$3
    local max_retries=$4
    local retry_interval=$5
    
    echo "Waiting for $service_name (${host}:${port})..."
    
    local retries=0
    while [ $retries -lt $max_retries ]; do
        if nc -z $host $port; then
            echo "$service_name is ready!"
            return 0
        fi
        
        echo "$service_name is not ready yet - waiting... ($((retries+1))/$max_retries)"
        sleep $retry_interval
        retries=$((retries+1))
    done
    
    echo "$service_name is not available after $max_retries attempts!"
    return 1
}

# Check PostgreSQL
if ! check_service db 5432 "PostgreSQL" 15 3; then
    echo "Cannot connect to PostgreSQL. Aborting startup."
    exit 1
fi

# Check Redis
if ! check_service redis 6379 "Redis" 10 2; then
    echo "Redis is not available. Cache will be disabled."
else
    # Check that Redis responds to PING
    if echo "PING" | nc -w 1 redis 6379 | grep -q "PONG"; then
        echo "Redis responds correctly to commands!"
    else
        echo "Redis does not respond correctly to commands. Cache might not function."
    fi
fi

# Initialize / migrate the database
echo "Applying database migrations..."
if ! command -v alembic &> /dev/null || [ ! -f "alembic.ini" ]; then
    echo "Alembic is required to initialize and migrate the schema."
    exit 1
fi
echo "Running 'alembic upgrade head'..."
alembic upgrade head
echo "Alembic migrations applied successfully!"

# Mode Batteries Included: Optional Seed
if [ "$SEED_ON_FIRST_RUN" = "true" ]; then
    ASSET_COUNT=$(python -c "
from database.service import DatabaseService
from models import Asset
try:
    db = DatabaseService().get_session()
    count = db.query(Asset).count()
    db.close()
    print(count)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

    if [ "${ASSET_COUNT:-0}" -eq 0 ]; then
        echo "SEED_ON_FIRST_RUN=true: Empty database, importing initial data..."
        python import_assets.py --file data/etf.csv
        echo "Initial data imported successfully!"
    else
        echo "Database already populated (${ASSET_COUNT} assets in database), skipping seed."
    fi
fi

# Start the application
echo "Starting the application..."
WEB_CONCURRENCY=${WEB_CONCURRENCY:-1}
exec gunicorn --bind 0.0.0.0:5000 --workers "$WEB_CONCURRENCY" --worker-class uvicorn.workers.UvicornWorker --timeout 120 main:app
