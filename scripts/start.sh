#!/usr/bin/env bash
# start.sh — starts a tiny HTTP healthcheck server on $PORT (so Render sees
# the bot web service as healthy), then runs the Telegram bot polling loop
# in the foreground (which keeps the container alive forever).
#
# Render free tier does NOT have Background Workers, so we deploy the bot
# as a regular Web Service. The healthcheck endpoint at GET /health
# returns 200 OK to satisfy Render.
#
# IMPORTANT: We respond to BOTH GET and HEAD. UptimeRobot (and many other
# monitors) send HEAD requests to be efficient — only responding to GET
# would yield 501 Not Implemented and falsely mark the service as down.

set -e
cd /app

PORT="${PORT:-10000}"
echo "[bot] $(date -u) — starting healthcheck HTTP on port ${PORT}"

# 1. Tiny HTTP server (background) — handles GET and HEAD
python3 - <<PYHTTP &
import http.server, os
PORT = int(os.environ.get("PORT", "10000"))
class H(http.server.BaseHTTPRequestHandler):
    def _respond(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "6")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"bot ok")
        else:
            self.send_response(404)
            self.end_headers()
    def do_GET(self):
        self._respond()
    def do_HEAD(self):
        self._respond()
    def log_message(self, *a, **k): pass
http.server.HTTPServer(("0.0.0.0", PORT), H).serve_forever()
PYHTTP

# 2. Telegram bot polling loop (foreground — keeps container alive)
echo "[bot] $(date -u) — launching Telegram bot polling loop"
exec python3 -m bot.bot
