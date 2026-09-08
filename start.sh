#!/bin/bash
set -e

echo "=========================================="
echo "  AlgoMirror - Starting Application"
echo "=========================================="

# Activate virtual environment
source /app/.venv/bin/activate

# Initialize database if not exists
echo "[1/3] Initializing database..."
python -c "
from app import create_app, db
app = create_app('production')
with app.app_context():
    db.create_all()
    print('Database initialized successfully')
"

# Check CSS
if [ -f /app/app/static/css/compiled.css ]; then
    echo "[2/3] CSS compiled and ready"
else
    echo "[2/3] Warning: compiled.css not found - styles may not load correctly"
fi

# Start the application with gunicorn
echo "[3/3] Starting Gunicorn server on port 8000..."
echo "=========================================="

# workers MUST stay at 1: background monitors (risk manager, pollers, exit monitors)
# start inside create_app() with no cross-worker singleton guard, so a second
# worker duplicates every monitor and can place duplicate exit orders.
exec gunicorn \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 16 \
    --worker-class gthread \
    --timeout 120 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --capture-output \
    "app:create_app('production')"
