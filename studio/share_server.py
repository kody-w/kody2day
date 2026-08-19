#!/usr/bin/env python3
"""share_server.py — serve finished Kody2day episodes on the private network (tailnet + LAN).

Same shape as the sentinel's tokenized reports: an unguessable folder per episode,
http://<tailscale-ip>:9798/share/<token>/  and  http://<lan-ip>:9798/share/<token>/
No directory listing, no path outside the share root, HTTP Range so iPhone Safari can
play (and scrub) the MP4s. Stdlib only.

    python3 share_server.py                    # serve ~/.rapp/kody2day-studio/share on :9798
    python3 share_server.py --publish DATE     # publish queue/<date> as a new token, print URLs
    python3 share_server.py --addresses        # print the private addresses this Mac answers on
"""

import argparse
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

STUDIO = Path(os.environ.get("KODY2DAY_STUDIO", "") or (Path.home() / ".rapp" / "kody2day-studio")).expanduser()
SHARE = STUDIO / "share"
PORT = int(os.environ.get("KODY2DAY_SHARE_PORT", "9798"))
TYPES = {".mp4": "video/mp4", ".html": "text/html; charset=utf-8", ".json": "application/json", ".png": "image/png", ".txt": "text/plain; charset=utf-8"}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def private_addresses():
    cands = []
    for ts in (shutil.which("tailscale"), "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
               "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if not ts or not Path(ts).exists():
            continue
        try:
            r = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                cands += r.stdout.split()
                break
        except Exception:
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            cands.append(s.getsockname()[0])
    except Exception:
        pass
    out = []
    for c in cands:
        try:
            a = ipaddress.ip_address(c.strip())
        except ValueError:
            continue
        if a.version == 4 and not a.is_loopback and str(a) not in out:
            out.append(str(a))
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SHARE), **kw)

    def log_message(self, *a):
        pass

    def _target(self):
        path = self.path.split("?")[0]
        if not path.startswith("/share/"):
            return None
        rel = path[len("/share/"):]
        if not rel or ".." in rel:
            return None
        t = (SHARE / rel).resolve()
        if SHARE.resolve() not in t.parents:
            return None
        if t.is_dir():
            t = t / "index.html"
        if not t.is_file() or t.suffix not in TYPES:
            return None
        return t

    def _serve(self, send_body):
        t = self._target()
        if t is None:
            self.send_error(404)
            return
        size = t.stat().st_size
        ctype = TYPES[t.suffix]
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = _RANGE.match(rng) if rng else None
        if m and t.suffix == ".mp4":
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        if not send_body:
            return
        with open(t, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def publish(date, max_age_days=14):
    """Copy queue/<date> into share/<token>/ with a player page; prune old tokens; return URLs."""
    q = STUDIO / "queue" / date
    if not q.exists():
        raise SystemExit("no queue for %s" % date)
    SHARE.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    for old in SHARE.glob("*/"):
        try:
            if now - old.stat().st_mtime > max_age_days * 86400:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass
    token = secrets.token_urlsafe(24)
    dst = SHARE / token
    dst.mkdir()
    yt = {}
    try:
        yt = json.loads((q / "YOUTUBE.json").read_text())
    except Exception:
        pass
    vids = sorted(q.glob("*.mp4"))
    for v in vids:
        try:
            os.link(v, dst / v.name)
        except Exception:
            shutil.copy2(v, dst / v.name)
    e = lambda s: html.escape(str(s or ""), quote=True)
    cards = []
    for v in vids:
        vertical = "short" in v.name
        cards.append("<div class=card><h2>%s</h2><video controls playsinline preload=metadata %s src='%s'></video>"
                     "<p><a href='%s' download>download %s</a></p></div>" % (
                         e(v.stem.replace("kody2day-", "Kody2day ")), "class=v" if vertical else "", e(v.name), e(v.name), e(v.name)))
    page = ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Kody2day %s</title><style>body{margin:0;background:#0b0d12;color:#e8eaf0;font:16px/1.5 -apple-system,system-ui,sans-serif}"
            "main{max-width:820px;margin:0 auto;padding:20px}h1{font-size:22px}.card{background:#141824;border:1px solid #232a3a;border-radius:12px;"
            "padding:14px;margin:0 0 16px}h2{font-size:16px;margin:0 0 8px}video{width:100%%;max-height:70vh;background:#000;border-radius:8px}"
            "video.v{max-width:360px;display:block;margin:0 auto}a{color:#7c9cff}p.d{color:#98a0b3;white-space:pre-wrap;font-size:14px}</style>"
            "<main><h1>%s</h1><p class=d>%s</p>%s<p class=d>private share — %s</p></main></html>") % (
        e(date), e(yt.get("title") or "Kody2day %s" % date), e(yt.get("description") or ""), "".join(cards),
        e(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    (dst / "index.html").write_text(page)
    urls = ["http://%s:%d/share/%s/" % (a, PORT, token) for a in private_addresses()]
    return {"date": date, "token": token, "dir": str(dst), "urls": urls, "files": [v.name for v in vids]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publish", metavar="DATE")
    ap.add_argument("--addresses", action="store_true")
    a = ap.parse_args(argv)
    if a.addresses:
        print(json.dumps(private_addresses()))
        return 0
    if a.publish:
        print(json.dumps(publish(a.publish), indent=1))
        return 0
    SHARE.mkdir(parents=True, exist_ok=True)
    srv = Server(("0.0.0.0", PORT), Handler)
    print("kody2day share on :%d (%s)" % (PORT, ", ".join(private_addresses())), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
