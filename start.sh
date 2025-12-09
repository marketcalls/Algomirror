#!/bin/bash
set -e

echo "=========================================="
echo "  AlgoMirror - Starting Application"
echo "=========================================="

# Activate virtual environment
source /app/.venv/bin/activate

# Initialize database if not exists
echo "[1/3] Initializing database..."
python -c "from app import create_app, db; app = create_app(); app.app_context().push(); db.create_all(); print('Database initialized')"

# Build CSS if needed (skip in production as it should be pre-built)
if [ ! -f /app/app/static/css/compiled.css ]; then
    echo "[2/3] CSS not found - using pre-built styles"
else
    echo "[2/3] CSS compiled and ready"
fi

# Start the application with gunicorn
echo "[3/3] Starting Gunicorn server on port 8000..."
echo "=========================================="

exec gunicorn \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --worker-class gthread \
    --timeout 120 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --capture-output \
    "app:create_app()"
