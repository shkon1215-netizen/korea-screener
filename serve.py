"""Local server for the dashboard, so the Refresh button actually refreshes.

    python serve.py                       # KOSPI, naver source, opens a browser
    python serve.py --port 9000
    python serve.py --board BOTH --min-roe 8      # extra args go to main_kr.py

Why this exists: re-running the screen means scraping KIND and Naver and doing
the peer maths in pandas. A page opened from disk (file://) or published as an
Artifact has no way to do that - browsers cannot start processes, and the
Artifact sandbox blocks external hosts outright. So the button needs something
local listening. That is all this is.

Routes:
    GET  /             the dashboard, rendered from the current results
    GET  /api/data     the current run as JSON
    GET  /api/status   {running, step, error, finished_at}
    POST /api/refresh  starts a run in the background; 409 if one is going

Binds to 127.0.0.1 only. It runs your screener on your machine; it is not
meant to be reachable from anywhere else.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# One server, one board per entry. Each keeps its own results, its own refresh
# job and its own progress, so refreshing KOSDAQ never disturbs KOSPI.
BOARDS = {
    "kospi": {"label": "KOSPI", "stem": "kr_screen_results",
              "args": ["--board", "KOSPI", "--skip-liquidity", "--source", "naver",
                       "--all", "--out", "kr_screen_results.csv",
                       "--dashboard", "kr_dashboard.html"]},
    "kosdaq": {"label": "KOSDAQ", "stem": "kq_screen_results",
               "args": ["--board", "KOSDAQ", "--skip-liquidity", "--source", "naver",
                        "--all", "--out", "kq_screen_results.csv",
                        "--dashboard", "kq_dashboard.html"]},
}


def paths(slug: str) -> tuple[str, str]:
    stem = BOARDS[slug]["stem"]
    return (os.path.join(HERE, stem + ".csv"),
            os.path.join(HERE, stem + "_meta.json"))


def board_links(active: str) -> list[dict]:
    return [{"label": b["label"], "href": "/" + slug, "active": slug == active}
            for slug, b in BOARDS.items() if os.path.exists(paths(slug)[0])]

# main_kr.py logs progress; these turn its noise into something worth showing.
STEPS = [
    (re.compile(r"page (\d+), (\d+) rows so far"), "Reading listings — page {0} ({1} rows)"),
    (re.compile(r"listings over .* pages"), "Board listing complete…"),
    (re.compile(r"KIND .*industry rows"), "Fetching 업종 classifications…"),
    (re.compile(r"universe: .* corporate lines"), "Trimming ETFs and funds…"),
    (re.compile(r"cleared size/liquidity"), "Applying size gate…"),
    (re.compile(r"EV/EBITDA (\d+)/(\d+)"), "Fetching EV/EBITDA… {0}/{1}"),
    (re.compile(r"fetching industry"), "Enriching survivors…"),
]

JOBS = {slug: {"running": False, "step": "", "error": None, "finished_at": None}
        for slug in BOARDS}
LOCK = threading.Lock()
EXTRA_ARGS: list[str] = []


def _run_screen(slug: str) -> None:
    """Shell out to main_kr.py rather than importing it, so a crash mid-run
    cannot leave this process holding half-built pandas state."""
    job = JOBS[slug]
    csv_path, meta_path = paths(slug)
    cmd = [sys.executable, os.path.join(HERE, "main_kr.py"),
           *BOARDS[slug]["args"], *EXTRA_ARGS]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    tail: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            for pat, template in STEPS:
                m = pat.search(line)
                if m:
                    with LOCK:
                        job["step"] = template.format(*m.groups())
                    break
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"main_kr.py exited {code}: "
                               + (tail[-1] if tail else "no output"))
        if not (os.path.exists(csv_path) and os.path.exists(meta_path)):
            raise RuntimeError("the run produced no results file")
        err = None
    except Exception as e:                              # noqa: BLE001
        err = str(e)[:300]
    with LOCK:
        job.update(running=False, step="", error=err,
                   finished_at=time.strftime("%H:%M:%S"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                  # quieter console
        if "/api/status" not in (self.path or ""):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    # -- helpers ------------------------------------------------------
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _fail(self, msg: str, code: int = 500) -> None:
        self._json({"error": msg}, code)

    def _board(self) -> str:
        """Which board this request is about. Taken from ?board= for the API and
        from the path for pages; falls back to the first board with results."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        slug = (q.get("board") or [""])[0].lower()
        if slug in BOARDS:
            return slug
        seg = urllib.parse.urlparse(self.path).path.strip("/").lower()
        if seg in BOARDS:
            return seg
        for s in BOARDS:
            if os.path.exists(paths(s)[0]):
                return s
        return next(iter(BOARDS))

    # -- routes -------------------------------------------------------
    def do_GET(self):                                   # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        slug = self._board()
        csv_path, meta_path = paths(slug)
        try:
            if path == "/" or path.strip("/") in BOARDS:
                from dashboard import render_html
                if not os.path.exists(csv_path):
                    return self._send(
                        f"<h1>No results for {BOARDS[slug]['label']} yet</h1>"
                        f"<p>Run the screen for this board once first.</p>".encode(),
                        "text/html; charset=utf-8", 404)
                html = render_html(csv_path, meta_path, "standalone",
                                   boards=board_links(slug))
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/status":
                with LOCK:
                    return self._json(dict(JOBS[slug]))
            if path == "/api/data":
                from dashboard import build_payload
                return self._json(build_payload(csv_path, meta_path,
                                                board_links(slug)))
            if path == "/favicon.ico":
                return self._send(b"", "image/x-icon", 204)
            return self._fail("not found", 404)
        except Exception as e:                          # noqa: BLE001
            return self._fail(str(e)[:300])

    def do_POST(self):                                  # noqa: N802
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/api/refresh":
            return self._fail("not found", 404)
        slug = self._board()
        with LOCK:
            if JOBS[slug]["running"]:
                return self._json({"running": True}, 409)
            JOBS[slug].update(running=True, step="Starting…", error=None,
                              finished_at=None)
        threading.Thread(target=_run_screen, args=(slug,), daemon=True).start()
        return self._json({"started": True, "board": slug}, 202)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    a, rest = p.parse_known_args()

    global EXTRA_ARGS
    EXTRA_ARGS = rest          # e.g. --min-roe 8, applied to every board

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print("Korea screener dashboard")
    for slug, b in BOARDS.items():
        have = "ready" if os.path.exists(paths(slug)[0]) else "no results yet"
        print(f"  {b['label']:<7} {url}{slug}   ({have})")
    if EXTRA_ARGS:
        print(f"  extra screen args: {' '.join(EXTRA_ARGS)}")
    print("  ctrl-c to stop\n")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
