"""Exercise production forwarding through real Express and Uvicorn sockets."""
from __future__ import annotations

import json
import selectors
import shutil
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from config import settings
from services.request_security import client_ip, request_is_secure, same_origin_request


class ProxyChainRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = (settings.PUBLIC_URL, settings.SETUP_COMPLETE, settings.ALLOWED_ORIGINS, settings.TRUSTED_PROXY_CIDRS)
        settings.PUBLIC_URL = "http://localhost:3000"
        settings.SETUP_COMPLETE = False
        settings.ALLOWED_ORIGINS = []
        settings.TRUSTED_PROXY_CIDRS = ["127.0.0.1/32", "::1/128"]
        app = FastAPI()

        @app.post("/api/setup/check")
        async def check(request: Request, response: Response):
            response.set_cookie("streamhome_setup", "isolated", secure=request_is_secure(request))
            return {"sameOrigin": same_origin_request(request), "secure": request_is_secure(request), "client": client_ip(request), "peer": request.client.host, "forwardedProto": request.headers.get("x-forwarded-proto"), "forwarded": request.headers.get("forwarded")}

        cls.socket = socket.socket()
        cls.socket.bind(("127.0.0.1", 0))
        cls.socket.listen()
        cls.backend_url = f"http://127.0.0.1:{cls.socket.getsockname()[1]}"
        cls.backend = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_level="error", lifespan="off"))
        cls.thread = threading.Thread(target=cls.backend.run, kwargs={"sockets": [cls.socket]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.backend.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.backend.started:
            raise RuntimeError("Disposable Uvicorn test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.backend.should_exit = True
        cls.thread.join(timeout=5)
        cls.socket.close()
        settings.PUBLIC_URL, settings.SETUP_COMPLETE, settings.ALLOWED_ORIGINS, settings.TRUSTED_PROXY_CIDRS = cls.previous

    def through_frontend(self, headers, cidrs=""):
        project = Path(__file__).resolve().parents[2]
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        loader = project / "web/node_modules/tsx/dist/loader.mjs"
        process = subprocess.Popen([node, "--import", str(loader), str(Path(__file__).parent / "fixtures/proxy_frontend.mjs"), self.backend_url, cidrs], cwd=project / "web", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=5):
                    raise RuntimeError("Disposable Express proxy did not start")
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(process.stderr.read())
            port = json.loads(line)["port"]
            return httpx.post(f"http://127.0.0.1:{port}/api/setup/check", headers=headers, timeout=5)
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def test_direct_lan_setup_strips_forged_headers_and_keeps_origin(self):
        response = self.through_frontend({"Host": "192.168.1.25:3000", "Origin": "http://192.168.1.25:3000", "Cookie": "streamhome_setup=isolated", "X-Forwarded-For": "203.0.113.50", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "attacker.example", "Forwarded": "for=203.0.113.50;proto=https"})
        payload = response.json()
        self.assertTrue(payload["sameOrigin"])
        self.assertFalse(payload["secure"])
        self.assertEqual(payload["client"], "127.0.0.1")
        self.assertIsNone(payload["forwarded"])

    def test_https_chain_keeps_immediate_peer_and_secure_cookie(self):
        response = self.through_frontend({"Host": "localhost:3000", "Origin": "https://watch.example", "Cookie": "streamhome_setup=isolated", "X-Forwarded-For": "198.51.100.20, 127.0.0.1", "X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "watch.example"}, "127.0.0.1/32,::1/128")
        payload = response.json()
        self.assertTrue(payload["sameOrigin"])
        self.assertTrue(payload["secure"])
        self.assertEqual(payload["client"], "198.51.100.20")
        self.assertEqual(payload["peer"], "127.0.0.1")
        self.assertEqual(payload["forwardedProto"], "https")
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_spoofed_prefix_cannot_claim_https_or_origin_at_untrusted_hop(self):
        response = self.through_frontend({"Host": "watch.example", "Origin": "https://attacker.example", "Cookie": "streamhome_setup=isolated", "X-Forwarded-For": "203.0.113.50, 198.51.100.20", "X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "attacker.example, watch.example"}, "127.0.0.1/32")
        payload = response.json()
        self.assertFalse(payload["sameOrigin"])
        self.assertFalse(payload["secure"])
        self.assertEqual(payload["client"], "198.51.100.20")
        self.assertNotIn("Secure", response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
