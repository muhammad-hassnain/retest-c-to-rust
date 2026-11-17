#!/usr/bin/env python3
"""Serve the results explorer at http://127.0.0.1:8790 (explicit directory; no os.getcwd()).

Also serves the blind second-rating page (rate.html, built by scripts/build_rating_page.py) and its
answer store: GET /api/ratings returns the saved answers, POST /api/ratings {id, answer} saves one
case to out/second-rater/ratings.json (or $RATINGS_FILE). The author's recorded answers in
docs/reviews/second-rater/ratings.json are never written. Local only (binds 127.0.0.1).
"""
import http.server
import json
import os
import re
import socketserver
import threading
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
RATINGS = os.path.abspath(os.environ.get("RATINGS_FILE") or os.path.join(REPO, "out", "second-rater", "ratings.json"))
RECORDED = os.path.join(REPO, "docs", "reviews", "second-rater", "ratings.json")
if RATINGS == os.path.abspath(RECORDED):
    raise SystemExit("refusing to write the recorded ratings; set RATINGS_FILE to another path")
PORT = 8790
_lock = threading.Lock()


def _load():
    try:
        with open(RATINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _source(self):
        """GET /src?path=artifacts/...: one fetched third-party source file, for rate.html (read-only; paths under
        artifacts/ only). `...json#extracted_code` returns an AdaTrans conversation's last non-empty extracted_code."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("path", [""])[0]
        rel, _, frag = q.partition("#")
        parts = rel.split("/")
        if parts[0] != "artifacts" or ".." in parts or "" in parts or frag not in ("", "extracted_code"):
            return self._json(400, {"error": "path must be a file under artifacts/"})
        try:
            if frag:
                hist = json.load(open(os.path.join(REPO, rel))).get("history", [])
                text = [h.get("extracted_code") for h in hist if h.get("extracted_code")][-1]
            else:
                text = open(os.path.join(REPO, rel), errors="replace").read()
        except (OSError, ValueError, IndexError, AttributeError):
            return self._json(404, {"error": "not found: fetch the artifacts first (bash scripts/provision.sh artifacts)"})
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/src":
            return self._source()
        if self.path.split("?")[0].rstrip("/") == "/api/ratings":
            with _lock:
                return self._json(200, {"file": os.path.relpath(RATINGS, REPO), "ratings": _load()})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/api/ratings":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            cid, ans = msg.get("id"), msg.get("answer")
            if not (isinstance(cid, str) and re.fullmatch(r"item\d{2}", cid) and isinstance(ans, dict)):
                return self._json(400, {"error": "expected {id: 'itemNN', answer: {...}}"})
        except (ValueError, OSError):
            return self._json(400, {"error": "bad JSON"})
        with _lock:
            data = _load()
            data[cid] = ans
            os.makedirs(os.path.dirname(RATINGS), exist_ok=True)
            tmp = RATINGS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, RATINGS)
        return self._json(200, {"ok": True})


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Explorer: http://127.0.0.1:{PORT}/  (second rating: /rate.html)", flush=True)
        httpd.serve_forever()
