#!/usr/bin/env python3
"""Local memory atlas server. Re-reads the maps and raw logs on every page load, so it always
shows the current state. Stdlib only, binds to localhost only.

Usage: python3 remember/serve.py [--port 8765] [--root DIR]
Then open http://127.0.0.1:8765
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import viz  # noqa: E402
import memmap  # noqa: E402


import viewer as _viewer  # noqa: E402
VERSION = _viewer.code_version()  # the code this process loaded; `remember viewer start` replaces stale ones


def make_handler(root):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/ping":
                    self._send(200, json.dumps({"app": "remember.me", "pid": os.getpid(), "version": VERSION,
                                                "root": root}), "application/json")
                elif path in ("/", "/index.html"):
                    self._send(200, viz.page(None), "text/html; charset=utf-8")
                elif path == "/api/data":
                    memmap._LOG_CACHE.clear()
                    self._send(200, json.dumps(viz.build_data(root=root), ensure_ascii=False),
                               "application/json; charset=utf-8")
                else:
                    self._send(404, "not found", "text/plain")
            except Exception as e:  # show the problem instead of a blank page
                self._send(500, f"{type(e).__name__}: {e}", "text/plain")

        def do_POST(self):
            # map edits from the viewer: recorded as user events (timestamped now, pinned)
            if self.path.split("?", 1)[0] != "/api/op":
                return self._send(404, "not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                project, op = body["project"], body["op"]
                if op.get("op") not in ("node.update", "node.merge", "node.move"):
                    return self._send(400, "only node.update, node.merge and node.move are allowed from the viewer", "text/plain")
                memmap._LOG_CACHE.clear()
                (target, ev), = memmap.apply_ops(root, project, [op], by="user")
                g = memmap.replay(root, target)
                self._send(200, json.dumps({"ok": True, "seq": ev["seq"], "warnings": g["warnings"][-3:]}), "application/json")
            except Exception as e:
                self._send(400, f"{type(e).__name__}: {e}", "text/plain")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default=viz.ROOT, help="folder containing maps/ and log/")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(os.path.abspath(a.root)))
    print(f"remember.me atlas: http://127.0.0.1:{a.port}  (maps from {os.path.abspath(a.root)}; Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
