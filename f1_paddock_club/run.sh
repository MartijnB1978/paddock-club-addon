#!/bin/sh
# Paddock Club add-on entrypoint
set -e
OPTS=/data/options.json
opt() { python3 -c "import json,sys;print(json.load(open('$OPTS')).get('$1') or '')" 2>/dev/null; }

export F1POOL_DB=/data/f1pool.sqlite3 F1POOL_DATA=/data
export ADMIN_NAME="$(opt admin_name)" ADMIN_EMAIL="$(opt admin_email)"
export F1_USERNAME="$(opt f1_username)" F1_PASSWORD="$(opt f1_password)"
export SMTP_USER="$(opt smtp_user)" SMTP_PASSWORD="$(opt smtp_password)"
export TZ_DISPLAY="$(opt timezone)"
BASE_URL="$(opt base_url)"
TS_KEY="$(opt tailscale_authkey)"
TS_HOST="$(opt tailscale_hostname)"

# --- isolated Tailscale node (userspace: no access to the host network stack) ---
if [ -n "$TS_KEY" ] || [ -f /data/tailscale/tailscaled.state ]; then
  mkdir -p /data/tailscale /var/run/tailscale
  tailscaled --tun=userspace-networking --statedir=/data/tailscale \
             --socket=/var/run/tailscale/tailscaled.sock --port=0 &
  sleep 3
  if [ -n "$TS_KEY" ]; then
    tailscale up --authkey="$TS_KEY" --hostname="${TS_HOST:-f1}" \
                 --accept-routes=false --accept-dns=false || echo "tailscale up failed"
  else
    tailscale up --hostname="${TS_HOST:-f1}" --accept-routes=false --accept-dns=false || true
  fi
  # publish ONLY this app (port 8080) to the internet over HTTPS
  tailscale funnel --bg 8080 || echo "tailscale funnel failed (is Funnel allowed for this node in the tailnet policy?)"
  if [ -z "$BASE_URL" ]; then
    DNS=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
    [ -n "$DNS" ] && BASE_URL="https://$DNS"
  fi
  tailscale funnel status || true
fi
export BASE_URL
echo "Paddock Club starting; public address: ${BASE_URL:-not set}"
exec python3 -m uvicorn f1pool.main:app --host 0.0.0.0 --port 8080 \
     --proxy-headers --forwarded-allow-ips 127.0.0.1 --log-level warning
