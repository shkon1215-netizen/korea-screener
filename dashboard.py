"""Builds a self-contained HTML dashboard from a screener run.

Reads the CSV that main_kr.py writes plus its _meta.json sidecar, and emits one
HTML file with the data inlined - no server, no network, no build step. Open it
in a browser; re-run main_kr.py to refresh it in place.

Two output shapes:
  mode="standalone"  full document, for opening off disk (the default)
  mode="artifact"    fragment for the Artifact host, which supplies the
                     doctype/head/body wrapper itself

Colour comes from the validated reference palette rather than invented values:
status hues are the fixed good/warning/critical steps, the discount ramp is the
documented ordinal blue, and both are used on the surfaces those figures were
measured against. On the light surface `warning` sits at 1.79:1, so ROE is
always rendered as dot + number + tier word - the colour never carries the
meaning by itself.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

# Ordered funnel. Each entry is (meta key, label, note key or None).
FUNNEL_STEPS = [
    ("listings", "Corporate lines", "ETFs, ETNs and funds already removed"),
    ("after_korea_filters", "After share-class hygiene", None),
    ("cleared_size_liquidity", "Cleared size gate", None),
    ("after_industry_known", "Scored against peers", None),
    ("passing", "Cheap vs peers", None),
    ("passing_after_roe", "…and earning its keep", None),
]

# (key, header, "l" to left-align)
TABLE_COLS = [
    ("ticker", "Code", "l"), ("name", "Name", "l"), ("industry", "업종 industry", "l"),
    ("mcap_musd", "Cap $m", ""), ("trailing_pe", "PER", ""), ("price_to_book", "PBR", ""),
    ("ev_to_ebitda", "EV/EBITDA", ""), ("roe_pct", "ROE %", ""),
    ("div_yield", "Yield %", ""), ("avg_discount", "Discount", ""),
    ("screen", "Screen", "l"), ("metrics_passing", "Cheap on", "l"),
]

# The absolute screen as a checklist, so the page can show what each test costs.
ABS_TESTS = [
    ("abs_pbr_ok", "PBR below {abs_max_pbr:g}"),
    ("abs_ev_ok", "EV/EBITDA below {abs_max_ev_ebitda:g}"),
    ("abs_pbr_vs_roe_ok", "PBR below fair value (ROE ÷ {abs_cost_of_equity_pct:g}% CoE)"),
    ("abs_div_ok", "Dividend yield {abs_min_div_yield:g}% or better"),
    ("abs_roe_ok", "ROE {min_roe_pct:g}% or better"),
]


def _f(v, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _records(df: pd.DataFrame) -> list[dict]:
    out = []
    for _, r in df.iterrows():
        out.append({
            "ticker": str(r.get("ticker", "")),
            "name": str(r.get("name", "")),
            "board": str(r.get("board", "")),
            "industry": "" if pd.isna(r.get("industry")) else str(r.get("industry")),
            "mcap_musd": _f(r.get("market_cap_usd", np.nan) / 1e6, 0),
            "trailing_pe": _f(r.get("trailing_pe")),
            "price_to_book": _f(r.get("price_to_book")),
            "ev_to_ebitda": _f(r.get("ev_to_ebitda")),
            "roe_pct": _f(r.get("roe_pct"), 1),
            "div_yield": _f(r.get("div_yield")),
            "avg_discount": _f(r.get("avg_discount"), 4),
            "metrics_passing": "" if pd.isna(r.get("metrics_passing")) else str(r.get("metrics_passing")),
            # Per-metric discounts and the financial flag travel with every row
            # so the page can re-evaluate both screens against thresholds the
            # reader chooses, instead of only showing the run's own verdict.
            "disc_pe": _f(r.get("trailing_pe_discount"), 4),
            "disc_pb": _f(r.get("price_to_book_discount"), 4),
            "disc_ev": _f(r.get("ev_to_ebitda_discount"), 4),
            "fin": bool(str(r.get("sector", "") or "").lower().find("financial") >= 0),
            "roe_tier": str(r.get("roe_tier", "")),
            "passes": bool(r.get("passes", False)),
            "screen": str(r.get("screen", "") or ""),
            "passes_any": bool(r.get("passes_any", r.get("passes", False))),
            "abs_passes": bool(r.get("abs_passes", False)),
            "carveout": bool(r.get("abs_via_carveout", False)),
            "fair_pbr": _f(r.get("abs_fair_pbr")),
            "holdco": bool(r.get("is_holdco", False)),
            "pbr20": bool(r.get("pbr_bottom20_industry", False)),
            "pbr1": bool(r.get("pbr_below_1", False)),
        })
    return out


def _funnel(meta: dict) -> list[dict]:
    f = meta.get("funnel", {})
    steps, top = [], None
    for key, label, note in FUNNEL_STEPS:
        if key not in f:
            continue
        n = int(f[key])
        top = n if top is None else top
        steps.append({"label": label, "n": n, "note": note,
                      "pct": (n / top * 100.0) if top else 0.0})
    return steps


def _drops(meta: dict) -> list[dict]:
    f = meta.get("funnel", {})
    out = []
    for key, label in [("dropped_preferred", "우선주 preferred"),
                       ("dropped_reit", "리츠 REIT"),
                       ("dropped_spac", "스팩 SPAC")]:
        if f.get(key):
            out.append({"label": label, "n": int(f[key])})
    roe_key = next((k for k in f if k.startswith("dropped_roe_below_")), None)
    if roe_key and f.get(roe_key):
        out.append({"label": f"ROE below {roe_key.rsplit('_', 1)[1]}%", "n": int(f[roe_key])})
    return out


# Conventional dashboard filename per board, so a standalone file can link to
# its sibling on disk without a server.
BOARD_FILES = {"KOSPI": "kr_dashboard.html",
               "KOSDAQ": "kq_dashboard.html",
               "BOTH": "krkq_dashboard.html"}


def sibling_boards(current_board: str, out_path: str) -> list[dict]:
    """Board switcher entries for a standalone file: the current board, plus any
    other board whose dashboard has actually been built next to it. A link to a
    file that does not exist is worse than no link."""
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    here = os.path.basename(out_path)
    out = []
    for board, fname in BOARD_FILES.items():
        if board == current_board:
            out.append({"label": board, "href": here, "active": True})
        elif os.path.exists(os.path.join(out_dir, fname)):
            out.append({"label": board, "href": fname, "active": False})
    return out if len(out) > 1 else []


def build_payload(csv_path: str, meta_path: str, boards: list | None = None) -> dict:
    """Everything the page needs, as plain JSON. Split out from the HTML so the
    local server can hand back fresh data without re-sending the document."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype={"ticker": str})
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)

    th = meta.get("thresholds", {})
    rel = df[df.get("passes", False) == True]              # noqa: E712
    absol = df[df.get("abs_passes", False) == True]        # noqa: E712
    anyp = df[df.get("passes_any", df.get("passes", False)) == True]  # noqa: E712
    med_roe = anyp["roe_pct"].median() if len(anyp) else float("nan")
    med_disc = rel["avg_discount"].median() if len(rel) else float("nan")

    abs_tests = []
    for key, label in ABS_TESTS:
        if key in df.columns:
            abs_tests.append({"label": label.format(**th),
                              "n": int((df[key] == True).sum())})  # noqa: E712

    payload = {
        "rows": _records(df),
        "funnel": _funnel(meta),
        "drops": _drops(meta),
        "meta": {
            "asof": meta.get("asof", ""),
            "source": meta.get("source", ""),
            "board": meta.get("board", ""),
            "cmd": meta.get("cmd", ""),
            "generated_at": meta.get("generated_at", ""),
            "krw_per_usd": meta.get("krw_per_usd"),
            "skip_liquidity": bool(th.get("skip_liquidity")),
            "min_mcap_musd": round(float(th.get("min_mcap_usd", 0)) / 1e6),
            "discount": th.get("discount", 0.20),
            "min_metrics": th.get("min_metrics", 2),
            "min_peers": th.get("min_peers", 5),
            "min_valid": th.get("min_valid_metrics", 2),
            "min_roe": th.get("min_roe_pct", 5.0),
            "roe_good": th.get("roe_good_pct", 10.0),
            "n_total": int(len(df)),
            "n_passing": int(len(anyp)),
            "n_relative": int(len(rel)),
            "n_absolute": int(len(absol)),
            "n_both": int((df.get("screen", pd.Series(dtype=str)) == "both").sum()),
            "n_carveout": int((absol.get("abs_via_carveout", pd.Series(dtype=bool)) == True).sum()),  # noqa: E712
            "abs_max_pbr": th.get("abs_max_pbr", 1.0),
            "abs_max_ev": th.get("abs_max_ev_ebitda", 8.0),
            "coe": th.get("abs_cost_of_equity_pct", 10.0),
            "abs_min_div": th.get("abs_min_div_yield", 2.0),
            "med_roe": None if pd.isna(med_roe) else round(float(med_roe), 1),
            "med_disc": None if pd.isna(med_disc) else round(float(med_disc), 4),
        },
        "abs_tests": abs_tests,
        "boards": boards or [],
        "cols": [{"k": k, "h": h, "a": a} for k, h, a in TABLE_COLS],
    }
    return payload


# The page title is the artifact's identity in the gallery, so each board needs
# its own. KOSPI keeps the original name - renaming a published artifact makes
# it unrecognisable to anyone who bookmarked it.
BOARD_TITLES = {"KOSDAQ": "KOSDAQ Discount Screen"}


def render_html(csv_path: str, meta_path: str, mode: str = "standalone",
                boards: list | None = None, hint: str | None = None,
                noindex: bool = False) -> str:
    payload = build_payload(csv_path, meta_path, boards)
    title = BOARD_TITLES.get(payload["meta"].get("board", ""))
    if hint:
        payload["meta"]["static_hint"] = hint
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    body = _TEMPLATE.replace("/*__PAYLOAD__*/null", blob)
    head = _HEAD
    if noindex:
        # Unlisted, not secret: reachable by URL, skipped by search engines.
        head = ('<meta name="robots" content="noindex, nofollow">\n'
                '<meta name="referrer" content="no-referrer">\n' + head)
    if title:
        head = head.replace("<title>Korea Discount Screen</title>",
                            f"<title>{title}</title>", 1)
        body = body.replace("<h1>Korea Discount Screen</h1>",
                            f"<h1>{title}</h1>", 1)

    if mode == "standalone":
        html = ("<!doctype html>\n<html lang=\"en\">\n<head>\n"
                "<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                + head + "</head>\n<body>\n" + body + "\n</body>\n</html>\n")
    else:
        html = head + body
    return html


def build_dashboard(csv_path: str, meta_path: str, out_path: str,
                    mode: str = "standalone", boards: list | None = None,
                    hint: str | None = None, noindex: bool = False) -> str:
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(csv_path, meta_path, mode, boards, hint, noindex))
    return os.path.abspath(out_path)


_HEAD = """<title>Korea Discount Screen</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@300;400;500;600;700&display=swap">
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --raise:#ffffff;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#898781;
  --grid:#e1e0d9; --rule:#c3c2b7; --ring:rgba(11,11,11,.10);
  --accent:#2a78d6;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --d1:#86b6ef; --d2:#5598e7; --d3:#2a78d6; --d4:#1c5cab; --d5:#104281;
  --shadow:0 1px 2px rgba(11,11,11,.05), 0 8px 24px -16px rgba(11,11,11,.28);
  --sans:"IBM Plex Sans KR", system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
  --mono:"IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19; --raise:#20201f;
    --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#898781;
    --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,.12);
    --accent:#3987e5;
    --d1:#1c5cab; --d2:#256abf; --d3:#3987e5; --d4:#6da7ec; --d5:#9ec5f4;
    --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 24px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --raise:#20201f;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#898781;
  --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,.12);
  --accent:#3987e5;
  --d1:#1c5cab; --d2:#256abf; --d3:#3987e5; --d4:#6da7ec; --d5:#9ec5f4;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 24px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font-family:var(--sans);
  font-weight:400;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:32px 24px 72px;
  display:flex;flex-direction:column;gap:28px}
/* Flex and grid children default to min-width:auto, which lets the wide table
   and the nowrap refresh command push the page sideways. Every wrapper that
   contains scrollable content has to be allowed to shrink below it. */
.wrap>*{min-width:0}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-3);margin:0}
h1{font-size:clamp(28px,4vw,40px);font-weight:600;letter-spacing:-.02em;margin:6px 0 0;
  text-wrap:balance}
.sub{color:var(--ink-2);margin:10px 0 0;max-width:62ch;font-size:15px}
.boards{display:flex;gap:2px;margin:14px 0 0;padding:3px;border-radius:9px;
  background:var(--surface);border:1px solid var(--ring);width:fit-content}
.boards a{font-size:13px;font-weight:500;text-decoration:none;padding:6px 16px;
  border-radius:6px;color:var(--ink-2);transition:background .12s ease}
.boards a:hover{background:var(--plane);color:var(--ink)}
.boards a.on{background:var(--ink);color:var(--surface)}
.hdr-row{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;
  justify-content:space-between;border-bottom:1px solid var(--rule);padding-bottom:20px}
.hdr-row>*{min-width:0;max-width:100%}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{font-family:var(--mono);font-size:11px;padding:5px 9px;border-radius:4px;
  border:1px solid var(--ring);background:var(--surface);color:var(--ink-2);
  white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.refresh{background:var(--surface);border:1px solid var(--ring);border-radius:8px;
  padding:14px 16px;display:flex;flex-direction:column;gap:8px;min-width:0}
.refresh code{font-family:var(--mono);font-size:12px;color:var(--ink);
  background:var(--plane);border:1px solid var(--grid);border-radius:5px;
  padding:7px 10px;display:block;overflow-x:auto;white-space:nowrap}
.refresh code.inl{display:inline;padding:1px 5px;white-space:nowrap}
#rbtn{font-family:var(--sans);font-size:14px;font-weight:500;cursor:pointer;
  padding:9px 18px;border-radius:7px;border:1px solid transparent;
  background:var(--accent);color:#fff;transition:filter .15s ease}
#rbtn:hover:not(:disabled){filter:brightness(1.08)}
#rbtn:disabled{cursor:progress;opacity:.65}
#rbtn.err{background:var(--crit)}
.rstatus{margin:8px 0 0;font-size:12px;color:var(--ink-3);max-width:46ch}
.rstatus.work{color:var(--accent)}
.rstatus.bad{color:var(--crit)}
.spin{display:inline-block;width:11px;height:11px;margin-right:6px;
  border:2px solid currentColor;border-right-color:transparent;border-radius:50%;
  vertical-align:-1px;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.spin{animation:none}}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:16px 18px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:4px}
.tile .v{font-size:30px;font-weight:600;letter-spacing:-.02em;line-height:1.1}
.tile .k{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3)}
.tile .n{font-size:12px;color:var(--ink-2)}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  box-shadow:var(--shadow);overflow:hidden;min-width:0}
.panel-h{padding:16px 18px 0;display:flex;justify-content:space-between;
  align-items:baseline;gap:12px;flex-wrap:wrap}
.panel-h h2{font-size:14px;font-weight:600;margin:0;letter-spacing:-.01em}
.panel-h .hint{font-size:12px;color:var(--ink-3)}
.funnel{padding:14px 18px 18px;display:flex;flex-direction:column;gap:9px}
.fstep{display:grid;grid-template-columns:20px 1fr auto;gap:12px;align-items:center}
.fstep .idx{font-family:var(--mono);font-size:11px;color:var(--ink-3);text-align:right}
.ftrack{background:var(--grid);border-radius:3px;height:26px;position:relative;
  overflow:hidden}
.fbar{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:3px}
/* The last stages are a few percent of the first, so their bars are too narrow
   to hold a label. Below the threshold the count sits outside the bar in ink
   rather than being clipped by the track's overflow. */
.fval{position:absolute;top:50%;transform:translateY(-50%);font-size:12px;
  font-weight:500;white-space:nowrap;font-family:var(--mono);
  font-variant-numeric:tabular-nums}
.fval.in{color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.35)}
.fval.out{color:var(--ink)}
.flab{font-size:13px;color:var(--ink-2);white-space:nowrap}
.flab b{color:var(--ink);font-weight:600;font-family:var(--mono);
  font-variant-numeric:tabular-nums}
.fnote{grid-column:2/4;font-size:11.5px;color:var(--ink-3);margin-top:-3px}
.two{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
  align-items:start}
.two>*{min-width:0}
.tests{list-style:none;margin:0;padding:14px 18px 16px;display:flex;
  flex-direction:column;gap:10px}
.tests li{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:baseline;
  font-size:13px;color:var(--ink-2);border-bottom:1px solid var(--grid);
  padding-bottom:9px}
.tests li:last-child{border-bottom:0;padding-bottom:0}
.tests b{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);
  font-weight:600}
.tests .tick{color:var(--good);margin-right:7px;font-weight:700}
.tests li.total{border-top:1px solid var(--rule);padding-top:11px;margin-top:2px;
  color:var(--ink);font-weight:500}
.drops{border-top:1px solid var(--grid);padding:12px 18px;display:flex;
  flex-wrap:wrap;gap:14px}
.drop{font-size:12px;color:var(--ink-2)}
.drop b{font-family:var(--mono);color:var(--ink);font-weight:600}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;
  background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:14px 16px;box-shadow:var(--shadow)}
.ctl{display:flex;flex-direction:column;gap:5px}
.ctl label{font-size:10.5px;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3)}
.ctl input[type=search],.ctl select{font-family:var(--sans);font-size:13px;
  padding:7px 10px;border-radius:6px;border:1px solid var(--rule);
  background:var(--raise);color:var(--ink);min-width:150px}
.ctl input[type=range]{width:150px;accent-color:var(--accent)}
.ctl .rv{font-family:var(--mono);font-size:12px;color:var(--ink);
  font-variant-numeric:tabular-nums}
.toggle{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--ink-2);
  cursor:pointer;padding-bottom:7px}
.toggle input{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
.count{margin-left:auto;font-size:12px;color:var(--ink-3);padding-bottom:8px;
  font-family:var(--mono);font-variant-numeric:tabular-nums}
.thr .panel-h .hint{display:flex;align-items:center;gap:10px}
#thrreset{font-family:var(--sans);font-size:11px;font-weight:600;cursor:pointer;
  padding:4px 10px;border-radius:5px;border:1px solid var(--ring);
  background:var(--plane);color:var(--ink-2)}
#thrreset:hover{color:var(--ink);border-color:var(--rule)}
#thrstate.edited{color:var(--accent);font-weight:600}
.thrgrid{padding:16px 18px 4px;display:grid;gap:12px 16px;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.tg{display:flex;flex-direction:column;gap:5px;min-width:0}
.tg label{font-size:10.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-3)}
.tg input{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;
  padding:7px 10px;border-radius:6px;border:1px solid var(--rule);
  background:var(--raise);color:var(--ink);width:100%}
.tg input:focus{border-color:var(--accent);outline:none}
.tg input.off{color:var(--ink-3)}
.thrtoggles{padding:6px 18px 16px;display:flex;flex-wrap:wrap;gap:18px;
  align-items:center}
.thrtoggles .toggle{padding-bottom:0}
.thrnote{font-size:11.5px;color:var(--ink-3);margin-left:auto}
.tscroll{overflow-x:auto;min-width:0;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:13px}
thead th{position:sticky;top:0;z-index:2;background:var(--surface);
  border-bottom:1px solid var(--rule);text-align:right;padding:10px 12px;
  font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-3);white-space:nowrap;cursor:pointer;user-select:none}
thead th.l{text-align:left}
thead th:hover{color:var(--ink)}
thead th .ar{opacity:0;margin-left:4px}
thead th.on{color:var(--ink)} thead th.on .ar{opacity:1}
tbody td{border-bottom:1px solid var(--grid);padding:9px 12px;text-align:right;
  font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tbody td.l{font-family:var(--sans);text-align:left;white-space:normal;
  word-break:keep-all}
tbody td:nth-child(2){min-width:150px}
tbody td:nth-child(3){min-width:130px}
.sbadge{font-size:9.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  padding:3px 7px;border-radius:4px;white-space:nowrap;border:1px solid transparent}
.sbadge.relative{background:color-mix(in srgb,var(--accent) 14%,transparent);
  color:var(--accent);border-color:color-mix(in srgb,var(--accent) 32%,transparent)}
.sbadge.absolute{background:color-mix(in srgb,var(--good) 14%,transparent);
  color:var(--good);border-color:color-mix(in srgb,var(--good) 32%,transparent)}
.sbadge.both{background:var(--ink);color:var(--surface)}
.sbadge.none{color:var(--ink-3);border-color:var(--ring)}
tbody tr:hover{background:var(--plane)}
tbody tr.miss{opacity:.55}
/* Korean breaks at any character by default, so 현대지에프홀딩스 shatters across
   four lines in a narrow column. keep-all breaks at word boundaries instead. */
.nm{display:flex;align-items:center;gap:6px;font-weight:500;flex-wrap:wrap}
.nmt{white-space:nowrap}
.tag{font-size:9.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  padding:2px 5px;border-radius:3px;border:1px solid var(--ring);color:var(--ink-3);
  white-space:nowrap}
.ind{font-size:12px;color:var(--ink-2)}
.roe{display:inline-flex;align-items:center;gap:6px;justify-content:flex-end}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.strong,.dot.good{background:var(--good)}
.dot.marginal{background:var(--warn)}
.dot.fail{background:var(--crit)}
.tier{font-family:var(--sans);font-size:10px;font-weight:600;letter-spacing:.05em;
  text-transform:uppercase;color:var(--ink-3);min-width:56px;text-align:left}
.dbar{position:relative;display:inline-block;min-width:88px;text-align:right}
.dbar i{position:absolute;left:0;top:50%;transform:translateY(-50%);height:16px;
  border-radius:2px;opacity:.9}
.dbar u{position:relative;text-decoration:none;padding-right:2px}
.dbar.neg u{color:var(--ink-3)}
.na{color:var(--ink-3)}
.notes{display:flex;flex-direction:column;gap:12px;border-top:1px solid var(--rule);
  padding-top:20px}
.notes h3{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-3);margin:0}
.notes p{margin:0;font-size:13px;color:var(--ink-2);max-width:78ch}
.notes b{color:var(--ink);font-weight:600}
.empty{padding:40px;text-align:center;color:var(--ink-3);font-size:14px}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
"""


_TEMPLATE = """
<div class="wrap">

  <header>
    <div class="hdr-row">
      <div>
        <p class="eyebrow">KRX relative valuation</p>
        <h1>Korea Discount Screen</h1>
        <nav class="boards" id="boards" hidden></nav>
        <p class="sub">Names trading below their industry-peer median that still
        earn a return. Cheapness alone is arithmetic; the ROE floor is what
        separates a mispricing from a company priced correctly for not earning
        its cost of capital.</p>
      </div>
      <div class="refresh">
        <p class="eyebrow" style="margin:0">Refresh</p>
        <div id="rlive" hidden>
          <button id="rbtn" type="button">Re-run screen</button>
          <p class="rstatus" id="rstatus">Re-runs the screen against live data. About a minute.</p>
        </div>
        <div id="rstatic">
          <code id="cmd">python main_kr.py</code>
          <p class="rstatus" id="rhint">This copy is a snapshot. Double-click
            <code class="inl">dashboard.cmd</code> for a working button.</p>
        </div>
      </div>
    </div>
    <div class="chips" id="chips" style="margin-top:16px"></div>
  </header>

  <section class="tiles" id="tiles"></section>

  <div class="two">
    <section class="panel">
      <div class="panel-h">
        <h2>How the universe narrows</h2>
        <span class="hint">relative screen, in order</span>
      </div>
      <div class="funnel" id="funnel"></div>
      <div class="drops" id="drops"></div>
    </section>

    <section class="panel">
      <div class="panel-h">
        <h2>Absolute value tests</h2>
        <span class="hint">all must hold</span>
      </div>
      <ul class="tests" id="tests"></ul>
      <div class="drops" id="absnote"></div>
    </section>
  </div>

  <section class="controls">
    <div class="ctl">
      <label for="q">Search</label>
      <input type="search" id="q" placeholder="name, code or 업종">
    </div>
    <div class="ctl">
      <label for="scr">Screen</label>
      <select id="scr">
        <option value="">Either screen</option>
        <option value="relative">Relative only</option>
        <option value="absolute">Absolute only</option>
        <option value="both">Both screens</option>
      </select>
    </div>
    <div class="ctl">
      <label for="brd">Board</label>
      <select id="brd"><option value="">All</option></select>
    </div>
    <label class="toggle"><input type="checkbox" id="onlypass" checked> Passing only</label>
    <label class="toggle"><input type="checkbox" id="nohold"> Hide holdcos</label>
    <span class="count" id="count"></span>
  </section>

  <section class="panel thr">
    <div class="panel-h">
      <h2>Thresholds</h2>
      <span class="hint">
        <span id="thrstate">showing the published run</span>
        <button type="button" id="thrreset">Reset</button>
      </span>
    </div>
    <div class="thrgrid">
      <div class="tg"><label for="t_mcap_lo">Market cap min $m</label>
        <input type="number" id="t_mcap_lo" min="0" step="50"></div>
      <div class="tg"><label for="t_mcap_hi">Market cap max $m</label>
        <input type="number" id="t_mcap_hi" min="0" step="50" placeholder="none"></div>
      <div class="tg"><label for="t_pbr">PBR below</label>
        <input type="number" id="t_pbr" min="0" step="0.1"></div>
      <div class="tg"><label for="t_ev">EV/EBITDA below</label>
        <input type="number" id="t_ev" min="0" step="0.5"></div>
      <div class="tg"><label for="t_per">PER below</label>
        <input type="number" id="t_per" min="0" step="1" placeholder="off"></div>
      <div class="tg"><label for="t_roe">ROE at least %</label>
        <input type="number" id="t_roe" step="1"></div>
      <div class="tg"><label for="t_div">Dividend at least %</label>
        <input type="number" id="t_div" min="0" step="0.5"></div>
      <div class="tg"><label for="t_coe">Cost of equity %</label>
        <input type="number" id="t_coe" min="1" step="0.5"></div>
      <div class="tg"><label for="t_disc">Discount at least %</label>
        <input type="number" id="t_disc" step="5"></div>
      <div class="tg"><label for="t_nmet">…on at least N metrics</label>
        <input type="number" id="t_nmet" min="1" max="3" step="1"></div>
    </div>
    <div class="thrtoggles">
      <label class="toggle"><input type="checkbox" id="t_fair"> Require PBR below fair value</label>
      <label class="toggle"><input type="checkbox" id="t_carve"> Financials qualify on PBR + ROE</label>
      <span class="thrnote" id="mcapnote"></span>
    </div>
  </section>

  <section class="panel">
    <div class="tscroll">
      <table>
        <thead><tr id="thead"></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="empty" id="empty" hidden>Nothing matches these filters.</div>
  </section>

  <section class="notes">
    <h3>Read this before acting on it</h3>
    <p><b>Two screens, deliberately independent.</b> The <b>relative</b> screen asks
    whether a name is cheap against its own industry peers. The <b>absolute</b> screen
    ignores the neighbours and asks whether it is cheap outright. Korea needs both:
    a peer group where everything is expensive still produces "cheap" names, and one
    where everything is cheap hides them. A row tagged <b>both</b> cleared each test
    on its own terms.</p>
    <p><b>Financials qualify on a weaker bar.</b> Enterprise value is meaningless for
    a bank, so EV/EBITDA is suppressed for them — which means a strict
    both-metrics rule would exclude every bank, insurer, broker and holdco no matter
    how far below book it traded. Those names clear the absolute screen on PBR and
    ROE alone and are marked <span class="tag">pbr+roe</span>.</p>
    <p><b>Low PBR with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Much of what gets called the Korea
    Discount is this. The ROE floor removes the worst of it, but a name just
    above the line is still worth checking by hand.</p>
    <p><b>업종 files holding companies under 기타 금융업.</b> That pools operating
    holdcos with bank holdcos in one peer group, and suppresses EV/EBITDA for
    both — enterprise value is meaningless for a bank, but not for an operating
    company. Holdcos are tagged so you can see which rows this touches.</p>
    <p><b id="livenote"></b></p>
    <p>Peer medians are winsorized and exclude the stock itself. A metric with
    fewer than <span id="mp"></span> peers is left unscored rather than
    benchmarked against noise. Research tool, not investment advice.</p>
  </section>
</div>

<script>
/* D is replaced wholesale when the server hands back a fresh run, so nothing
   below may capture its contents at load time - everything reads through the
   live binding and repaints. */
let D = /*__PAYLOAD__*/null;
let M = D.meta;
const $ = (id) => document.getElementById(id);

/* ---- header chips ---------------------------------------------------- */
function paintHeader() {
const bs = D.boards || [];
$("boards").hidden = bs.length < 2;
$("boards").innerHTML = bs.map(b =>
  `<a href="${b.href}" class="${b.active ? "on" : ""}">${b.label}</a>`).join("");

$("cmd").textContent = M.cmd || ("python main_kr.py --board " + (M.board || "BOTH")
  + (M.skip_liquidity ? " --skip-liquidity" : "") + " --all");

const chips = [
  ["As of", M.asof],
  ["Source", M.source === "naver" ? "KIND + Naver" : "KRX"],
  ["Board", M.board],
  ["Built", M.generated_at],
  ["USD/KRW", M.krw_per_usd],
];
$("chips").innerHTML = chips.filter(c => c[1] !== null && c[1] !== undefined && c[1] !== "")
  .map(c => `<span class="chip">${c[0]} <b>${c[1]}</b></span>`).join("");

if (M.static_hint) {
  // Hosted build: there is no local server to offer, and saying so beats
  // pointing at a command the reader cannot run.
  $("rhint").textContent = M.static_hint;
  $("cmd").hidden = true;
}

$("livenote").textContent = M.source === "naver"
  ? "Multiples are live, not as of the " + M.asof + " close, and traded value is a "
    + "volume x close proxy with one session of history — which is why the liquidity "
    + "gate is skipped on this source rather than trusted."
  : "Multiples are as of the " + M.asof + " close.";
$("mp").textContent = M.min_peers;
}

/* ---- tiles ----------------------------------------------------------- */
function median(xs) {
  const v = xs.filter(x => x !== null && x !== undefined).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

function paintTiles() {
const live = D.rows.filter(r => r.cap);
const anyp = live.filter(r => r.ev.any);
const rel = live.filter(r => r.ev.rel), absl = live.filter(r => r.ev.abs);
const both = live.filter(r => r.ev.screen === "both");
const mRoe = median(anyp.map(r => r.roe_pct));
const mDisc = median(rel.map(r => r.avg_discount));
const capLabel = T.mcap_hi ? `$${T.mcap_lo ?? 0}m–$${T.mcap_hi}m`
                           : `$${T.mcap_lo ?? 0}m+`;
const tiles = [
  {k:"Passing either", v:anyp.length,
   n:`${rel.length} relative · ${absl.length} absolute · ${both.length} both`},
  {k:"Median discount", v:mDisc===null?"—":(mDisc*100).toFixed(0)+"%",
   n:`relative: ${T.disc===null?"any":T.disc+"%"}+ below peers on ${T.nmet}+ metrics`},
  {k:"Median ROE", v:mRoe===null?"—":mRoe.toFixed(1)+"%",
   n:T.roe===null?"no ROE floor":`floor ${T.roe}%, target ${M.roe_good}%+`},
  {k:"Market cap", v:capLabel, n:`${live.length} of ${D.rows.length} names in range`},
];
$("tiles").innerHTML = tiles.map(t =>
  `<div class="tile"><span class="k">${t.k}</span><span class="v">${t.v}</span>
   <span class="n">${t.n}</span></div>`).join("");
}

/* ---- funnel ---------------------------------------------------------- */
function paintFunnel() {
/* The first stages describe what Python did and are fixed. The last three
   depend on the current thresholds, so they are recomputed - otherwise the
   funnel would contradict the tiles above it. */
const live = D.rows.filter(r => r.cap);
const overrides = {
  "Cleared size gate": live.length,
  "Scored against peers": live.length,
  "Cheap vs peers": live.filter(r => r.ev.rel).length,
  "…and earning its keep": live.filter(r => r.ev.any).length,
};
const top = D.funnel.length ? D.funnel[0].n : 1;
$("funnel").innerHTML = D.funnel.map((s0, i) => {
  const n = overrides[s0.label] !== undefined ? overrides[s0.label] : s0.n;
  const s = {...s0, n, pct: top ? n / top * 100 : 0};
  const w = Math.max(s.pct, 4), inside = w >= 18;
  return `
  <div class="fstep">
    <span class="idx">${i + 1}</span>
    <div class="ftrack">
      <div class="fbar" style="width:${w}%"></div>
      <span class="fval ${inside ? "in" : "out"}"
            style="left:${inside ? "10px" : `calc(${w}% + 8px)`}">${s.n.toLocaleString()}</span>
    </div>
    <span class="flab">${s.label}</span>
    ${s.note ? `<span class="fnote">${s.note}</span>` : ""}
  </div>`;
}).join("");

$("drops").innerHTML = D.drops.length
  ? '<span class="drop" style="color:var(--ink-3)">Removed along the way:</span>'
    + D.drops.map(d => `<span class="drop"><b>${d.n}</b> ${d.label}</span>`).join("")
  : "";
}

/* ---- absolute tests --------------------------------------------------- */
function paintTests() {
  const live = D.rows.filter(r => r.cap);
  const n = (f) => live.filter(f).length;
  const tests = [];
  if (T.pbr !== null) tests.push(["PBR below " + T.pbr, n(r => r.ev.tests.pbr)]);
  if (T.ev !== null) tests.push(["EV/EBITDA below " + T.ev, n(r => r.ev.tests.ev)]);
  if (T.per !== null) tests.push(["PER below " + T.per,
    n(r => r.trailing_pe !== null && r.trailing_pe < T.per)]);
  if (T.fair) tests.push([`PBR below fair value (ROE ÷ ${T.coe}% CoE)`,
    n(r => r.ev.tests.fair)]);
  if (T.div !== null) tests.push(["Dividend yield " + T.div + "% or better",
    n(r => r.ev.tests.div)]);
  if (T.roe !== null) tests.push(["ROE " + T.roe + "% or better", n(r => r.ev.tests.roe)]);

  $("tests").innerHTML = (tests.length
    ? tests.map(([l, c]) =>
        `<li><span><span class="tick">✓</span>${l}</span><b>${c}</b></li>`).join("")
    : '<li><span style="color:var(--ink-3)">No absolute tests active</span><b>—</b></li>')
    + `<li class="total"><span>All of them together</span>`
    + `<b>${n(r => r.ev.abs)}</b></li>`;

  const carved = n(r => r.ev.abs && r.ev.carved);
  $("absnote").innerHTML = carved
    ? `<span class="drop"><b>${carved}</b> financials qualified on PBR and ROE `
      + `alone — they have no EV/EBITDA to test</span>`
    : "";
}

/* Board options are rebuilt from the new data, but a selection the viewer made
   is kept when that board still exists in the refreshed run. */
function paintBoards() {
  const prev = $("brd").value;
  const boards = [...new Set(D.rows.map(r => r.board))].filter(Boolean).sort();
  $("brd").innerHTML = '<option value="">All</option>'
    + boards.map(b => `<option value="${b}">${b}</option>`).join("");
  if (boards.includes(prev)) $("brd").value = prev;
  $("brd").closest(".ctl").hidden = boards.length < 2;
}

function paintAll() {
  evaluateAll();
  paintHeader(); paintTiles(); paintFunnel(); paintTests(); paintBoards(); render();
}

/* ---- table ----------------------------------------------------------- */
const NUM = new Set(["mcap_musd","trailing_pe","price_to_book","ev_to_ebitda",
                     "roe_pct","div_yield","avg_discount"]);
let sortKey = "avg_discount", sortDir = -1;

$("thead").innerHTML = D.cols.map(c =>
  `<th class="${c.a}" data-k="${c.k}">${c.h}<span class="ar">▾</span></th>`).join("");
$("thead").querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.k;
  if (k === sortKey) sortDir = -sortDir; else { sortKey = k; sortDir = NUM.has(k) ? -1 : 1; }
  render();
}));

/* ---- thresholds: the screen re-evaluated in the browser ---------------
   Every input both screens need travels with each row, so changing a number
   here re-runs the verdict without re-running Python. Two things genuinely
   cannot be recomputed and are therefore not offered: peer medians (fixed
   when the run built its cohorts) and any market cap BELOW the run's floor,
   because those rows were gated out before scoring and are simply absent. */
const THR_KEYS = ["mcap_lo","mcap_hi","pbr","ev","per","roe","div","coe",
                  "disc","nmet","fair","carve"];
const STORE = "kr-thresholds-" + (M.board || "x");
let T = {};

function defaults() {
  return {
    mcap_lo: M.min_mcap_musd, mcap_hi: null,
    pbr: M.abs_max_pbr, ev: M.abs_max_ev, per: null,
    roe: M.min_roe, div: M.abs_min_div, coe: M.coe,
    disc: Math.round(M.discount * 100), nmet: M.min_metrics,
    fair: true, carve: (M.n_carveout || 0) > 0 || true,
  };
}

const numOrNull = (el) => el.value.trim() === "" ? null : Number(el.value);

function readControls() {
  T = {
    mcap_lo: numOrNull($("t_mcap_lo")), mcap_hi: numOrNull($("t_mcap_hi")),
    pbr: numOrNull($("t_pbr")), ev: numOrNull($("t_ev")),
    per: numOrNull($("t_per")), roe: numOrNull($("t_roe")),
    div: numOrNull($("t_div")), coe: numOrNull($("t_coe")) || 10,
    disc: numOrNull($("t_disc")), nmet: numOrNull($("t_nmet")) || 1,
    fair: $("t_fair").checked, carve: $("t_carve").checked,
  };
  ["t_per","t_mcap_hi"].forEach(id => $(id).classList.toggle("off", !numOrNull($(id))));
  try { localStorage.setItem(STORE, JSON.stringify(T)); } catch (e) {}
  const d = defaults();
  const edited = THR_KEYS.some(k => String(T[k]) !== String(d[k]));
  $("thrstate").textContent = edited ? "modified — not the published screen"
                                     : "showing the published run";
  $("thrstate").classList.toggle("edited", edited);
}

function writeControls(v) {
  $("t_mcap_lo").value = v.mcap_lo ?? "";
  $("t_mcap_hi").value = v.mcap_hi ?? "";
  $("t_pbr").value = v.pbr ?? "";
  $("t_ev").value = v.ev ?? "";
  $("t_per").value = v.per ?? "";
  $("t_roe").value = v.roe ?? "";
  $("t_div").value = v.div ?? "";
  $("t_coe").value = v.coe ?? 10;
  $("t_disc").value = v.disc ?? "";
  $("t_nmet").value = v.nmet ?? 2;
  $("t_fair").checked = !!v.fair;
  $("t_carve").checked = !!v.carve;
}

/* One row against the current thresholds. Mirrors korea_filters.apply_roe_gate
   and apply_absolute_screen, including the rule that a missing value fails a
   test it is subject to - unknown is not the same as passing. */
function evaluate(r) {
  const ds = [r.disc_pe, r.disc_pb, r.disc_ev].filter(d => d !== null);
  const nPass = T.disc === null ? ds.length
              : ds.filter(d => d >= T.disc / 100).length;
  const roeOk = T.roe === null || (r.roe_pct !== null && r.roe_pct >= T.roe);
  const rel = ds.length >= (M.min_valid || 2) && nPass >= T.nmet && roeOk;

  const pbrOk = T.pbr === null || (r.price_to_book !== null && r.price_to_book < T.pbr);
  const evOk  = T.ev  === null || (r.ev_to_ebitda !== null && r.ev_to_ebitda < T.ev);
  const perOk = T.per === null || (r.trailing_pe !== null && r.trailing_pe < T.per);
  const divOk = T.div === null || (r.div_yield !== null && r.div_yield >= T.div);
  const fairOk = !T.fair || (r.price_to_book !== null && r.roe_pct !== null
                             && r.price_to_book < r.roe_pct / T.coe);
  let core = pbrOk && evOk;
  let carved = false;
  if (T.carve && r.fin && pbrOk && !evOk) { core = true; carved = true; }
  const abs = core && perOk && roeOk && divOk && fairOk;

  return {rel, abs, carved,
          screen: rel && abs ? "both" : rel ? "relative" : abs ? "absolute" : "",
          any: rel || abs,
          tests: {pbr: pbrOk, ev: evOk, fair: fairOk, div: divOk, roe: roeOk}};
}

function inCap(r) {
  if (T.mcap_lo !== null && (r.mcap_musd === null || r.mcap_musd < T.mcap_lo)) return false;
  if (T.mcap_hi !== null && (r.mcap_musd === null || r.mcap_musd > T.mcap_hi)) return false;
  return true;
}

/* Start from the published run's own thresholds, unless this browser already
   has a saved set for this board. */
(function initThresholds() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) {}
  writeControls(saved || defaults());
  readControls();
  $("thrreset").addEventListener("click", () => {
    writeControls(defaults()); readControls(); paintAll();
  });
  THR_KEYS.forEach(k => $("t_" + k).addEventListener("input", () => {
    readControls(); paintAll();
  }));
  $("mcapnote").textContent =
    "Below $" + M.min_mcap_musd + "m there is no data — those names were gated "
    + "out before scoring. Re-run with a lower --min-mcap to reach further down.";
})();

function discColor(d) {
  if (d === null || d < 0) return "var(--ink-3)";
  const p = d * 100;
  return p < 30 ? "var(--d1)" : p < 40 ? "var(--d2)" : p < 50 ? "var(--d3)"
       : p < 60 ? "var(--d4)" : "var(--d5)";
}
const esc = (s) => String(s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function cell(r, k) {
  const v = r[k];
  if (k === "ticker") return `<span style="color:var(--ink-3)">${esc(v)}</span>`;
  if (k === "name") {
    const tags = (r.holdco ? '<span class="tag">holdco</span>' : "")
               + (r.pbr20 ? '<span class="tag">pbr b20</span>' : "");
    return `<span class="nm"><span class="nmt">${esc(v)}</span>${tags}</span>`;
  }
  if (k === "industry") return `<span class="ind">${esc(v || "—")}</span>`;
  if (k === "screen") {
    const s = r.ev.screen || "none";
    const lab = s === "none" ? "—" : s;
    const star = r.ev.carved ? ' <span class="tag">pbr+roe</span>' : "";
    return `<span class="sbadge ${s}">${lab}</span>${star}`;
  }
  if (k === "metrics_passing") return v ? esc(v) : '<span class="na">—</span>';
  if (k === "roe_pct") {
    if (v === null) return '<span class="na">—</span>';
    const t = r.roe_tier || (v >= 15 ? "strong" : v >= M.roe_good ? "good"
              : v >= M.min_roe ? "marginal" : "fail");
    return `<span class="roe"><span class="tier">${t}</span>
      <span class="dot ${t}"></span>${v.toFixed(1)}</span>`;
  }
  if (k === "avg_discount") {
    if (v === null) return '<span class="na">—</span>';
    const p = v * 100, w = Math.max(0, Math.min(100, p));
    return `<span class="dbar ${p < 0 ? "neg" : ""}">
      <i style="width:${w}%;background:${discColor(v)}"></i>
      <u>${p.toFixed(1)}%</u></span>`;
  }
  if (v === null) return '<span class="na">—</span>';
  if (k === "mcap_musd") return v.toLocaleString();
  return v.toFixed(2);
}

/* Re-evaluated on every threshold change and cached on the row, so the tiles,
   the tests panel, the funnel tail and the table all read one verdict. */
function evaluateAll() {
  D.rows.forEach(r => { r.ev = evaluate(r); r.cap = inCap(r); });
}

function render() {
  const q = $("q").value.trim().toLowerCase();
  const brd = $("brd").value;
  const onlyPass = $("onlypass").checked, noHold = $("nohold").checked;
  const scr = $("scr").value;

  let rows = D.rows.filter(r => {
    if (!r.cap) return false;
    if (onlyPass && !r.ev.any) return false;
    if (scr && r.ev.screen !== scr) return false;
    if (noHold && r.holdco) return false;
    if (brd && r.board !== brd) return false;
    if (q && !(r.name.toLowerCase().includes(q) || r.ticker.includes(q)
        || (r.industry || "").toLowerCase().includes(q))) return false;
    return true;
  });

  rows.sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ko")) * sortDir;
  });

  $("thead").querySelectorAll("th").forEach(th => {
    th.classList.toggle("on", th.dataset.k === sortKey);
    const ar = th.querySelector(".ar");
    if (ar) ar.textContent = sortDir < 0 ? "▾" : "▴";
  });

  $("tbody").innerHTML = rows.map(r =>
    `<tr class="${r.ev.any ? "" : "miss"}">`
    + D.cols.map(c => `<td class="${c.a}">${cell(r, c.k)}</td>`).join("") + "</tr>").join("");
  $("empty").hidden = rows.length > 0;
  const inCapN = D.rows.filter(r => r.cap).length;
  $("count").textContent = rows.length + " of " + inCapN + " shown"
    + (inCapN < D.rows.length ? ` (${D.rows.length - inCapN} outside the cap range)` : "");
}

["q","brd","scr","onlypass","nohold"].forEach(id =>
  $(id).addEventListener("input", render));

/* ---- live refresh ----------------------------------------------------
   Only serve.py can actually re-run the screen: it shells out to main_kr.py,
   which scrapes KIND and Naver. A page opened straight off disk, or published
   as an Artifact, has no such backend - so the button is shown only once
   /api/status answers, and the command line is shown otherwise. */
const POLL_MS = 1500;
let polling = null;
/* One server can host several boards, so every call names its own. Absolute
   paths, because the page is served at /kospi or /kosdaq and a relative URL
   would resolve differently per board. */
const API = (p) => "/api/" + p + (M.board ? "?board=" + encodeURIComponent(M.board.toLowerCase()) : "");

function setStatus(text, cls) {
  const el = $("rstatus");
  el.className = "rstatus" + (cls ? " " + cls : "");
  el.innerHTML = (cls === "work" ? '<span class="spin"></span>' : "") + text;
}

async function loadData() {
  const r = await fetch(API("data"), {cache: "no-store"});
  if (!r.ok) throw new Error("could not read the refreshed run");
  D = await r.json();
  M = D.meta;
  paintAll();
}

async function poll() {
  let s;
  try {
    s = await (await fetch(API("status"), {cache: "no-store"})).json();
  } catch (e) { return; }
  if (s.running) { setStatus(s.step || "Working…", "work"); return; }

  clearInterval(polling); polling = null;
  $("rbtn").disabled = false;
  if (s.error) {
    $("rbtn").classList.add("err");
    $("rbtn").textContent = "Try again";
    setStatus("Refresh failed: " + s.error, "bad");
    return;
  }
  $("rbtn").classList.remove("err");
  $("rbtn").textContent = "Re-run screen";
  try {
    await loadData();
    setStatus("Updated " + M.generated_at + ".", "");
  } catch (e) {
    setStatus(e.message, "bad");
  }
}

async function startRefresh() {
  $("rbtn").disabled = true;
  $("rbtn").classList.remove("err");
  setStatus("Starting…", "work");
  try {
    const r = await fetch(API("refresh"), {method: "POST"});
    if (r.status === 409) { setStatus("A refresh is already running.", "work"); }
    else if (!r.ok) throw new Error("server refused the request");
  } catch (e) {
    $("rbtn").disabled = false;
    setStatus("Could not reach the local server. Is serve.py still running?", "bad");
    return;
  }
  if (!polling) polling = setInterval(poll, POLL_MS);
}

(async function initRefresh() {
  try {
    const r = await fetch(API("status"), {cache: "no-store"});
    if (!r.ok) return;
    const s = await r.json();
    $("rstatic").hidden = true;
    $("rlive").hidden = false;
    $("rbtn").addEventListener("click", startRefresh);
    if (s.running) {
      $("rbtn").disabled = true;
      setStatus(s.step || "Working…", "work");
      polling = setInterval(poll, POLL_MS);
    }
  } catch (e) { /* no backend: the command line stays visible */ }
})();

paintAll();
</script>
"""
