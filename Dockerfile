# ------------------------------ Builder Stage ------------------------------ #
FROM python:3.12-bullseye AS builder

# Install build dependencies including TA-Lib
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential wget && \
    # Install TA-Lib C library
    wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files
COPY pyproject.toml .
COPY requirements.txt .

# Create isolated virtual-env with uv, then install dependencies
RUN pip install --no-cache-dir uv && \
    uv venv .venv && \
    . .venv/bin/activate && \
    uv pip install --upgrade pip && \
    uv pip install -r requirements.txt && \
    uv pip install gunicorn && \
    rm -rf /root/.cache

# --------------------------------------------------------------------------- #
# ------------------------------ Production Stage --------------------------- #
FROM python:3.12-slim-bullseye AS production

# 0 - Install runtime dependencies and TA-Lib
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata wget build-essential && \
    # Install TA-Lib C library (required at runtime)
    wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz && \
    # Set timezone to IST (Asia/Kolkata)
    ln -fs /usr/share/zoneinfo/Asia/Kolkata /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    # Cleanup
    apt-get remove -y wget build-essential && \
    apt-get autoremove -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 1 - Create non-root user and workdir
RUN useradd --create-home appuser
WORKDIR /app

# 2 - Copy the ready-made venv from builder
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# 3 - Copy application source with correct ownership
COPY --chown=appuser:appuser . .

# 4 - Create required directories with proper ownership and permissions
RUN mkdir -p /app/logs /app/instance /app/flask_session /app/migrations && \
    chown -R appuser:appuser /app/logs /app/instance /app/flask_session /app/migrations && \
    chmod -R 755 /app/logs /app/instance /app/flask_session && \
    # Create empty .env file with write permissions
    touch /app/.env && chown appuser:appuser /app/.env && chmod 666 /app/.env

# 5 - Entrypoint script and fix line endings
COPY --chown=appuser:appuser start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# ---- RUNTIME ENVS --------------------------------------------------------- #
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    FLASK_APP=app:create_app \
    FLASK_ENV=production

# --------------------------------------------------------------------------- #
USER appuser
EXPOSE 8000

CMD ["/app/start.sh"]
