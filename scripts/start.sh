#!/usr/bin/env bash
# start.sh — starts a tiny HTTP healthcheck server on $PORT (so Render sees
# the bot web service as healthy), then runs the Telegram bot polling loop
# in the foreground (which keeps the container alive forever).
#
# Render free tier does NOT have Background Workers, so we deploy the bot
# as a regular Web Service. The healthcheck endpoint at GET /health
# returns 200 OK to satisfy Render.

set -e
cd /app

PORT="${PORT:-10000}"
echo "[bot] $(date -u) — starting healthcheck HTTP on port ${PORT}"

# 1. Tiny HTTP server (background)
python3 - <<PYHTTP &
import http.server, os
PORT = int(os.environ.get("PORT", "10000"))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"bot ok")
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *a, **k): pass
http.server.HTTPServer(("0.0.0.0", PORT), H).serve_forever()
PYHTTP

# 2. Telegram bot polling loop (foreground — keeps container alive)
echo "[bot] $(date -u) — launching Telegram bot polling loop"
exec python3 -m bot.bot
