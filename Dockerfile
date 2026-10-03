# ==========================================
# 1. Builder Stage (Compilation of dependencies)
# ==========================================
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install compilation tools needed for compiling Python dependencies
RUN sed -i 's/deb.debian.org/ftp.fr.debian.org/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's/deb.debian.org/ftp.fr.debian.org/g' /etc/apt/sources.list 2>/dev/null && \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a clean virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies inside virtual env: the exact versions recorded
# in the lock file, each checked against its hash. Two builds of the same
# commit therefore contain the same packages (refresh the lock with `make lock`).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# ==========================================
# 2. Runner Stage (Lightweight production image)
# ==========================================
FROM python:3.12-slim-bookworm AS runner

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV NO_PROXY="localhost,127.0.0.1"
ENV PATH="/opt/venv/bin:$PATH"

# Create a non-root user for security
RUN groupadd -r fonrex && useradd -r -g fonrex fonrex

WORKDIR /app

# Install ONLY runtime system dependencies (no gcc or build tools)
RUN sed -i 's/deb.debian.org/ftp.fr.debian.org/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's/deb.debian.org/ftp.fr.debian.org/g' /etc/apt/sources.list 2>/dev/null && \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    netcat-openbsd \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application code with correct ownership directly (prevents layer duplication)
COPY --chown=fonrex:fonrex . .

# Convert line endings, make entrypoint executable and create the directories
# written at runtime (Compose mounts volumes over them)
RUN sed -i 's/\r$//' entrypoint.sh && \
    chmod +x entrypoint.sh && \
    mkdir -p logs static/logos && \
    chown fonrex:fonrex logs static static/logos

# Switch to non-root user
USER fonrex

# Expose port
EXPOSE 5000

# Docker healthcheck command
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Startup command
CMD ["./entrypoint.sh"]
