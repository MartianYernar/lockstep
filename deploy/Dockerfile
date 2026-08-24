FROM python:3.12-slim

# tzdata so the container agrees with you about what "16:00" means.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/

# The database, photos and config are bind-mounted, so the image stays stateless
# and you can rebuild it without ever putting the commitment at risk.
VOLUME ["/data"]
ENV LOCKSTEP_DB=/data/lockstep.db \
    LOCKSTEP_CONFIG=/data/config.yaml \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

EXPOSE 8765
CMD ["python", "-m", "server.main"]
