#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-shot setup for a fresh Ubuntu VPS. Paste this on the server:
#
#   curl -fsSL https://raw.githubusercontent.com/MartianYernar/lockstep/main/deploy/bootstrap.sh | bash
#
# ...except don't: that repo is private, and piping a URL straight into bash is
# a bad habit anyway. Clone the repo and run this file instead:
#
#   git clone https://github.com/MartianYernar/lockstep.git /opt/lockstep
#   cd /opt/lockstep && sudo bash deploy/bootstrap.sh
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/lockstep}"
DATA_DIR="$REPO_DIR/deploy/data"

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
[[ -f "$REPO_DIR/deploy/Dockerfile" ]] || { echo "Expected the repo at $REPO_DIR"; exit 1; }

say "Installing docker + sqlite"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 sqlite3 >/dev/null
systemctl enable --now docker

say "Opening port 8765 (the phone polls this)"
# Ubuntu images on Oracle ship a restrictive iptables chain that silently
# swallows traffic even after you open the port in the cloud console.
iptables -I INPUT -p tcp --dport 8765 -j ACCEPT 2>/dev/null || true
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save >/dev/null 2>&1 || true
else
  apt-get install -y -qq iptables-persistent >/dev/null 2>&1 || true
fi

say "Preparing $DATA_DIR"
mkdir -p "$DATA_DIR/photos"
if [[ ! -f "$DATA_DIR/config.yaml" ]]; then
  cp "$REPO_DIR/config.example.yaml" "$DATA_DIR/config.yaml"
  echo "  Created $DATA_DIR/config.yaml from the example."
  echo "  EDIT IT NOW before starting: bot token, chat ids, gemini key, api token."
  echo
  echo "  Generate an api.token with:"
  echo "    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
  exit 0
fi
chmod 600 "$DATA_DIR/config.yaml"

say "Building and starting"
cd "$REPO_DIR/deploy"
docker compose up -d --build

say "Waiting for health"
for i in $(seq 1 30); do
  sleep 2
  if curl -fsS --max-time 3 http://127.0.0.1:8765/health >/dev/null 2>&1; then
    echo "  healthy"
    break
  fi
done

say "Nightly backups"
CRON="0 3 * * * $REPO_DIR/deploy/backup.sh >> /var/log/lockstep-backup.log 2>&1"
( crontab -l 2>/dev/null | grep -Fv "lockstep/deploy/backup.sh"; echo "$CRON" ) | crontab -

IP=$(curl -fsS --max-time 5 ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")
say "Done"
cat <<EOF

  Server URL for the phone:   http://$IP:8765
  Guardian setup code:        docker compose logs | grep guardian
  Follow the logs:            docker compose logs -f
  Restart:                    docker compose restart
  It restarts itself on reboot and on crash.

EOF
