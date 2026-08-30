"""Offline validation of the Korea screener. No network required.

Run: python test_korea.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import korea_filters as KF
from config_kr import ScreenConfig
from screener import run_screen

rng = np.random.default_rng(7)

KRW_USD = 1.0 / 1386.0
MCAP = 1.5e12          # ~USD 1.08bn, clears the gate
ADV = 2.0e10           # ~USD 14m, clears the gate


def base(**kw):
    r = dict(name="", board="KOSPI", sector="Industrials", industry="Machinery",
             market_cap_local=MCAP, adv_local=ADV, close_krw=50_000,
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=5_000.0, book_value_ps=40_000.0, div_yield=1.5)
    r.update(kw)
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    # Two cohorts with deliberately different multiple levels by board.
    specs = [
        ("Machinery", "KOSPI", 11.0, 0.85, 6.0, 18),
        ("Biotechnology", "KOSDAQ", 38.0, 4.5, 26.0, 16),
    ]
    n = 0
    for industry, board, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(
                ticker=f"{100000 + n * 10:06d}", name=f"피어{n}",
                board=board, industry=industry,
                sector="Health Care" if board == "KOSDAQ" else "Industrials",
                trailing_pe=pe * rng.uniform(0.93, 1.09),
                price_to_book=pb * rng.uniform(0.93, 1.09),
                ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                market_cap_local=MCAP * rng.uniform(0.9, 2.5),
                adv_local=ADV * rng.uniform(0.9, 2.5)))

    # --- planted cases ------------------------------------------------
    # PASS: genuinely cheap vs KOSPI machinery, decent ROE, pays dividend
    rows.append(base(ticker="900010", name="진짜저평가",
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=4.0,
                     trailing_eps=6_600, book_value_ps=90_000, div_yield=4.2))
    # FAIL: preferred line of the above. 40% below its own common, which is
    # a share-class artifact, not a discount to the business.
    rows.append(base(ticker="900015", name="진짜저평가우",
                     trailing_pe=4.5, price_to_book=0.33, ev_to_ebitda=4.0))
    # FAIL: 2nd-preferred (new style), same trap
    rows.append(base(ticker="900017", name="진짜저평가2우B",
                     trailing_pe=4.4, price_to_book=0.32, ev_to_ebitda=4.0))
    # FLAGGED not dropped: holdco at permanent NAV discount
    rows.append(base(ticker="900020", name="대한지주",
                     trailing_pe=6.0, price_to_book=0.30, ev_to_ebitda=3.5,
                     trailing_eps=3_000, book_value_ps=150_000, div_yield=2.0))
    # FAIL: SPAC shell
    rows.append(base(ticker="900030", name="케이비제25호스팩", board="KOSDAQ",
                     industry="Biotechnology",
                     trailing_pe=3.0, price_to_book=0.4, ev_to_ebitda=2.0))
    # FAIL: REIT
    rows.append(base(ticker="900040", name="에스케이리츠",
                     trailing_pe=8.0, price_to_book=0.5, ev_to_ebitda=5.0))
    # FAIL: KRX reports PER=0 for a loss-maker. Must read as missing, not cheap.
    rows.append(base(ticker="900050", name="적자기업",
                     trailing_pe=0.0, price_to_book=0.60, ev_to_ebitda=0.0,
                     trailing_eps=-2_000))
    # FAIL: cheap KOSDAQ biotech vs KOSPI machinery, but normal for its board.
    # This is the board-mixing trap: 30x PE looks expensive against machinery
    # at 11x, and 26x looks cheap against biotech at 38x.
    rows.append(base(ticker="900060", name="바이오적정가", board="KOSDAQ",
                     sector="Health Care", industry="Biotechnology",
                     trailing_pe=36.0, price_to_book=4.3, ev_to_ebitda=25.0))
    # PASS: genuinely cheap KOSDAQ biotech vs its own board cohort
    rows.append(base(ticker="900070", name="바이오저평가", board="KOSDAQ",
                     sector="Health Care", industry="Biotechnology",
                     trailing_pe=27.0, price_to_book=3.2, ev_to_ebitda=19.0))
    # FAIL: too small
    rows.append(base(ticker="900080", name="소형주", market_cap_local=3e11,
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=4.0))
    # FAIL: illiquid
    rows.append(base(ticker="900090", name="비유동주", adv_local=1e9,
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=4.0))
    return pd.DataFrame(rows)


def main() -> int:
    cfg = ScreenConfig()
    df = make_universe()

    df, kstats = KF.apply_korea_filters(df, cfg)
    res, stats = run_screen(df, KRW_USD, cfg)
    res = KF.add_quality_context(res)
    res = KF.add_valueup_flags(res)

    print("=== funnel ===")
    for k, v in {**kstats, **stats}.items():
        print(f"  {k:<26} {v}")

    idx = res.set_index("ticker")
    expected = {
        "900010": True,   "900070": True,
        "900015": False,  "900017": False, "900030": False, "900040": False,
        "900050": False,  "900060": False, "900080": False, "900090": False,
    }

    print("\n=== planted cases ===")
    failures = []
    for t, want in expected.items():
        present = t in idx.index
        got = bool(idx.loc[t, "passes"]) if present else False
        ok = got == want
        why = "" if present else "  (removed before scoring)"
        print(f"  {'OK  ' if ok else 'FAIL'} {t}  expected={want!s:<5} got={got!s:<5}{why}")
        if not ok:
            failures.append(t)

    # Holdco must survive as flagged, not be silently dropped.
    if "900020" not in idx.index:
        failures.append("holdco_was_dropped")
    elif not bool(idx.loc["900020", "is_holdco"]):
        failures.append("holdco_not_flagged")
    else:
        print(f"  OK   900020  holdco retained and flagged "
              f"(passes={bool(idx.loc['900020','passes'])})")

    # Board separation: peer medians must differ sharply by board.
    kospi = res[(res["board"] == "KOSPI") & res["trailing_pe_peer_median"].notna()]
    kosdaq = res[(res["board"] == "KOSDAQ") & res["trailing_pe_peer_median"].notna()]
    if not kospi.empty and not kosdaq.empty:
        km, qm = kospi["trailing_pe_peer_median"].median(), kosdaq["trailing_pe_peer_median"].median()
        print(f"\n  KOSPI machinery peer PER: {km:.1f}   KOSDAQ biotech peer PER: {qm:.1f}")
        if not (qm > km + 10):
            failures.append("board_separation")
    else:
        failures.append("board_cohorts_missing")

    print("\n=== passing ===")
    hits = res[res["passes"]][["name", "board", "industry", "trailing_pe",
                               "price_to_book", "roe_pct", "div_yield",
                               "avg_discount", "pbr_bottom20_industry"]]
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
