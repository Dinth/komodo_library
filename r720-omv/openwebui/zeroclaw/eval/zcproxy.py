"""
Purpose: Logging reverse proxy between a test ZeroClaw instance and Ollama.
         Records every request body + response timing/usage to /log/req-N.json
         so prompt size, tool lists and latency can be analysed.
Dependencies: Python 3 stdlib only.
Author: AI (Claude)
"""
import http.client
import http.server
import json
import os
import threading
import time

UPSTREAM = os.environ.get("UPSTREAM", "ollama:11434")
LOG = "/log"
counter = 0
lock = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, method):
        global counter
        with lock:
            counter += 1
            n = counter
        body = b""
        if "Content-Length" in self.headers:
            body = self.rfile.read(int(self.headers["Content-Length"]))
        t0 = time.time()
        conn = http.client.HTTPConnection(UPSTREAM, timeout=900)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
        conn.request(method, self.path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        t1 = time.time()
        rec = {"n": n, "method": method, "path": self.path, "status": resp.status,
               "seconds": round(t1 - t0, 2), "req_bytes": len(body)}
        try:
            rec["request"] = json.loads(body) if body else None
        except Exception:
            rec["request_raw"] = body[:2000].decode("utf-8", "replace")
        rec["response_raw"] = data[-6000:].decode("utf-8", "replace")
        with open(f"{LOG}/req-{n:03d}.json", "w") as f:
            json.dump(rec, f, indent=1)
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
