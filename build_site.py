"""Assemble the static site that GitHub Pages serves.

Reads whatever result files exist and writes a self-contained `site/` folder:

    site/index.html    KOSPI
    site/kosdaq.html   KOSDAQ
    site/robots.txt    disallow everything
    site/.nojekyll     stop Pages mangling the output

The published pages have no Refresh button - there is no Python behind them.
They are rebuilt on a schedule instead, so the honest thing is to say when the
data was built and when the next build lands, rather than showing a control
that cannot work. Run `python build_site.py` after the screens have run.
"""
from __future__ import annotations

import os
import shutil
import sys

from dashboard import build_dashboard

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")

# (board, results stem, output filename, label)
PAGES = [
    ("KOSPI", "kr_screen_results", "index.html", "KOSPI"),
    ("KOSDAQ", "kq_screen_results", "kosdaq.html", "KOSDAQ"),
]

HINT = ("Rebuilt automatically each weekday after the KRX close. "
        "For an on-demand run against live numbers, use the local copy.")

ROBOTS = "User-agent: *\nDisallow: /\n"


def main() -> int:
    available = [(b, stem, out, label) for b, stem, out, label in PAGES
                 if os.path.exists(os.path.join(HERE, stem + ".csv"))]
    if not available:
        print("no result files found - run main_kr.py first", file=sys.stderr)
        return 1

    # Empty the folder rather than delete it. On Windows a synced directory
    # (OneDrive) or a process with it as its cwd holds the directory handle, so
    # rmdir fails with access denied even when every file inside is removable.
    os.makedirs(SITE, exist_ok=True)
    for name in os.listdir(SITE):
        target = os.path.join(SITE, name)
        try:
            shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
        except OSError as e:
            print(f"  could not remove {name}: {e}", file=sys.stderr)

    # Only link to boards that actually built, so the switcher never dangles.
    for board, stem, out, _ in available:
        boards = [{"label": lb, "href": o, "active": o == out}
                  for _, _, o, lb in available]
        path = build_dashboard(
            os.path.join(HERE, stem + ".csv"),
            os.path.join(HERE, stem + "_meta.json"),
            os.path.join(SITE, out),
            mode="standalone",
            boards=boards if len(available) > 1 else [],
            hint=HINT,
            noindex=True,
        )
        print(f"  {board:<7} -> site/{out}  ({os.path.getsize(path):,} bytes)")

    with open(os.path.join(SITE, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write(ROBOTS)
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    print(f"\nsite/ ready ({len(available)} page(s)), noindex + robots.txt applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
