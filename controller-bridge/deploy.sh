#!/bin/bash
# Push the AR game + bridge to the Pi, restart jzbridge, verify it came back.
#
#   ./deploy.sh                 sync, restart, verify
#   ./deploy.sh --dry-run       show what rsync would change, touch nothing
#   CERTS=certs ./deploy.sh     also install certs/cert.pem + key.pem
#                               (mkcert, SANs 10.42.0.1 10.42.1.1 jenzombies)
#   GAME=/path/to/game          where the game lives (default: ../game)
#   PI=pi@10.42.1.1             the Pi over the wired link (default)
set -euo pipefail
PI=${PI:-pi@10.42.1.1}
HERE=$(cd "$(dirname "$0")" && pwd)
GAME=${GAME:-$(cd "$HERE/../game" 2>/dev/null && pwd || echo "$HOME/dev/jen-zombie/game")}
REMOTE=controller-bridge
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="-n"

[ -d "$GAME/www" ] || { echo "no game at $GAME/www (set GAME=)"; exit 2; }
echo "== game: $GAME  ->  $PI:$REMOTE"
rsync -az $DRY -v --delete --exclude '__pycache__' "$GAME/www/" "$PI:$REMOTE/www/" | grep -vE '^(sending|sent|total|$)' || true
rsync -az $DRY -v "$GAME/worlds.py" "$HERE/bridge.py" "$HERE/gameweb.py" \
      "$HERE/controller.html" "$HERE/game.html" "$PI:$REMOTE/" | grep -vE '^(sending|sent|total|$)' || true
if [ -n "${CERTS:-}" ]; then
  echo "== installing certificate from $CERTS (old one kept as *.selfsigned)"
  ssh "$PI" "cd $REMOTE && for f in cert.pem key.pem; do [ -f \$f.selfsigned ] || cp \$f \$f.selfsigned; done" 
  rsync -az $DRY -v "$CERTS/cert.pem" "$CERTS/key.pem" "$PI:$REMOTE/" | grep -vE '^(sending|sent|total|$)' || true
fi
[ -n "$DRY" ] && { echo "(dry run, nothing restarted)"; exit 0; }

echo "== restart + verify"
ssh "$PI" bash -s <<'REMOTE_EOF'
set -e
cd ~/controller-bridge
chmod 600 key.pem 2>/dev/null || true
python3 -m py_compile bridge.py gameweb.py worlds.py
sudo systemctl restart jzbridge
ok=0
for i in $(seq 1 15); do
  sleep 1
  if systemctl is-active --quiet jzbridge && curl -sk -o /dev/null https://127.0.0.1:8443/status; then ok=1; break; fi
done
echo "service: $(systemctl is-active jzbridge) after ${i}s"
[ $ok = 1 ] || { journalctl -u jzbridge -n 30 --no-pager; exit 1; }
echo "--- /status"; curl -sk https://127.0.0.1:8443/status; echo
echo "--- pages"
for p in / /stage3.html /js/controller.js /js/led.js /vendor/three.module.js /world /dashboard.html /test /generate_204; do
  printf "  %-26s " "$p"; curl -sk -o /dev/null -w "%{http_code} %{size_download}B %{time_total}s\n" "https://127.0.0.1:8443$p"
done
printf "  %-26s " "http :80 /"; curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" http://127.0.0.1/
echo "--- /events (first frame)"; timeout 2 curl -skN https://127.0.0.1:8443/events | head -1 || true
echo "--- journal"; journalctl -u jzbridge -n 8 --no-pager -o cat
REMOTE_EOF
