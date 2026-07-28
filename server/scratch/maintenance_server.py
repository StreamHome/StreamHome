from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAINTENANCE_PAGE = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="20">
  <title>StreamHome maintenance</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { min-height: 100vh; margin: 0; display: grid; place-items: center; background: #0b0807; color: #f4ebe7; }
    main { width: min(34rem, calc(100% - 3rem)); padding: 2rem; border: 1px solid #563226; border-radius: 1rem; background: #160f0d; box-shadow: 0 1.5rem 5rem #0008; }
    p { color: #c8b6ae; line-height: 1.6; }
    small { color: #ff6b3d; letter-spacing: .08em; text-transform: uppercase; }
  </style>
</head>
<body>
  <main>
    <small>StreamHome / Maintenance</small>
    <h1>A validated update is being installed.</h1>
    <p>The server and web client are being health-checked. This page refreshes automatically and StreamHome will return when both services are ready.</p>
  </main>
</body>
</html>
"""


class MaintenanceHandler(BaseHTTPRequestHandler):
    def _respond(self, include_body: bool) -> None:
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(MAINTENANCE_PAGE)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", "20")
        self.end_headers()
        if include_body:
            self.wfile.write(MAINTENANCE_PAGE)

    def do_GET(self) -> None:
        self._respond(True)

    def do_HEAD(self) -> None:
        self._respond(False)

    def log_message(self, format: str, *args: object) -> None:
        return


class MaintenanceServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporary StreamHome maintenance responder")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    server = MaintenanceServer(("0.0.0.0", args.port), MaintenanceHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
