"""Korea (KRX) relative-valuation screener.

  python main_kr.py                          # full KOSPI + KOSDAQ run
  python main_kr.py --board KOSPI            # KOSPI only
  python main_kr.py --include-preferred      # see why this is off by default
  python main_kr.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import pandas as pd

import korea_filters as KF
from config_kr import ScreenConfig
from providers_kr import KRXProvider
from providers_naver_kr import NaverKindProvider
from screener import run_screen


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="kr_screen_results.csv")
    p.add_argument("--board", choices=["KOSPI", "KOSDAQ", "BOTH"], default="BOTH")
    p.add_argument("--min-mcap", type=float, default=600e6, help="USD")
    p.add_argument("--min-adv", type=float, default=4e6, help="USD")
    p.add_argument("--discount", type=float, default=0.20)
    p.add_argument("--min-metrics", type=int, default=2)
    p.add_argument("--min-peers", type=int, default=5)
    p.add_argument("--peer-keys", default="industry,board")
    p.add_argument("--fx", type=float, help="KRW per USD (default: live)")
    p.add_argument("--include-preferred", action="store_true")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--adv-days", type=int, default=60)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--source", choices=["auto", "krx", "naver"], default="auto",
                   help="krx needs KRX_ID/KRX_PW; naver uses KIND+Naver; "
                        "auto picks krx when credentials are present")
    p.add_argument("--min-roe", type=float, default=5.0,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=1.0,
                   help="absolute screen: PBR below this")
    p.add_argument("--abs-ev", type=float, default=8.0,
                   help="absolute screen: EV/EBITDA below this")
    p.add_argument("--abs-strict-financials", action="store_true",
                   help="require EV/EBITDA of financials too (they have none, so "
                        "none will pass)")
    p.add_argument("--no-abs-roe", action="store_true",
                   help="do not apply the ROE floor to the absolute screen")
    p.add_argument("--no-abs-fair-pbr", action="store_true",
                   help="drop the PBR < ROE/CoE test")
    p.add_argument("--coe", type=float, default=10.0,
                   help="cost of equity %% for the fair-PBR test")
    p.add_argument("--abs-min-div", type=float, default=2.0,
                   help="absolute screen: dividend yield %% floor; 0 disables")
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-5-year-history screen")
    p.add_argument("--hist-discount", type=float, default=0.30,
                   help="own-history screen: discount to 5y median, 0.30 = 30%%")
    p.add_argument("--hist-min-metrics", type=int, default=2,
                   help="own-history screen: metrics that must clear it (1-3)")
    p.add_argument("--keep-admin-issue", action="store_true",
                   help="do not drop 관리종목")
    p.add_argument("--dashboard", default="kr_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    a = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("kr")

    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        exclude_admin_issue=not a.keep_admin_issue,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=tuple(k.strip() for k in a.peer_keys.split(",") if k.strip()),
        exclude_preferred=not a.include_preferred,
        exclude_holdcos=a.exclude_holdcos,
        adv_lookback_days=a.adv_days,
        boards=("KOSPI", "KOSDAQ") if a.board == "BOTH" else (a.board,),
    )

    source = a.source
    if source == "auto":
        source = "krx" if (os.getenv("KRX_ID") and os.getenv("KRX_PW")) else "naver"
        log.info("source auto-selected: %s", source)
    if source == "krx":
        prov = KRXProvider(cfg)
    else:
        # KIND + Naver. Live snapshot, industry from KRX 업종, traded value
        # reconstructed from volume x close - see providers_naver_kr docstring.
        log.warning("using the KIND+Naver source: multiples are LIVE, not as of "
                    "the session date, and ADV is a proxy")
        prov = NaverKindProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    if not days:
        log.error("could not resolve trading calendar")
        return 1
    asof = days[-1]
    log.info("as of %s", asof)

    # 1. roster + Korea share-class hygiene (cheap, no per-ticker calls)
    roster = prov.listing_roster(asof)
    if roster.empty:
        log.error("empty roster - check pykrx install and network")
        return 1
    roster, kstats = KF.apply_korea_filters(roster, cfg)

    roster, hstats = KF.apply_halt_filter(roster)
    kstats.update(hstats)

    if cfg.exclude_admin_issue:
        from providers_naver_kr import fetch_admin_issue_names
        roster, astats = KF.apply_admin_issue_filter(roster, fetch_admin_issue_names())
        kstats.update(astats)

    # 2. cross-sectional market cap + fundamentals: 2 calls for the market
    caps = prov.market_cap_snapshot(asof)
    fund = prov.fundamentals_snapshot(asof)
    df = roster.merge(caps, on="ticker", how="left").merge(fund, on="ticker", how="left")

    # 3. liquidity: one call per session covers every listing
    if not a.skip_liquidity:
        log.info("liquidity over %d sessions...", len(days))
        df = df.merge(prov.liquidity(days), on="ticker", how="left")
    else:
        df["adv_local"] = float("nan")

    krw_usd = (1.0 / a.fx) if a.fx else prov.krw_to_usd()
    log.info("FX: 1 USD = %.1f KRW", 1.0 / krw_usd)

    # 4. Pre-gate on size/liquidity BEFORE the slow per-ticker enrichment.
    #    This is the whole reason the run is minutes not hours.
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * krw_usd
    df["adv_usd"] = pd.to_numeric(df.get("adv_local"), errors="coerce") * krw_usd
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    log.info("%d of %d listings cleared size/liquidity", len(pre), len(df))
    kstats["cleared_size_liquidity"] = len(pre)
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 5. enrich survivors only
    log.info("fetching industry + EV/EBITDA for %d names...", len(pre))
    enr = prov.enrich(pre["ticker"].tolist())
    pre = pre.merge(enr, on="ticker", how="left")

    # Three-year history. Also per-ticker, so it belongs here with the other
    # slow work - after the size gate, on survivors only (invariant 7).
    if not a.no_financials:
        from providers_naver_kr import fetch_financials
        log.info("fetching 3y financials for %d names...", len(pre))
        fin = fetch_financials(pre["ticker"].tolist(), cache=prov.cache,
                               delay=cfg.request_delay, workers=cfg.max_workers)
        if not fin.empty:
            pre = pre.merge(fin, on="ticker", how="left")

    # Five filed years of PER/PBR/EV-EBITDA for the own-history screen. Also
    # per ticker, so it stays here after the size gate (invariant 7).
    if not a.no_history:
        from providers_naver_kr import fetch_valuation_history
        log.info("fetching 5y valuation history for %d names...", len(pre))
        vh = fetch_valuation_history(pre["ticker"].tolist(), cache=prov.cache,
                                     delay=cfg.request_delay, workers=cfg.max_workers)
        if not vh.empty:
            pre = pre.merge(vh, on="ticker", how="left")

        # PER and PBR against TODAY's price, not the fiscal year end Naver
        # struck its own at. EPS and BPS are the reported per-share figures, so
        # korea_filters' ROE (EPS/BPS) stays consistent with the multiples being
        # screened - the property CLAUDE.md wants from ROE.
        if "trailing_eps" in pre.columns:
            close = pd.to_numeric(pre["close_krw"], errors="coerce")
            eps = pd.to_numeric(pre["trailing_eps"], errors="coerce")
            bps = pd.to_numeric(pre["book_value_ps"], errors="coerce")
            dps = pd.to_numeric(pre.get("dps"), errors="coerce")
            pre["trailing_pe"] = (close / eps).where(eps > 0)
            pre["price_to_book"] = (close / bps).where(bps > 0)
            pre["div_yield"] = (dps / close * 100.0).where(close > 0)

    # 6. screen
    res, stats = run_screen(pre, krw_usd, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = KF.add_quality_context(res)
    res = KF.add_valueup_flags(res)
    if cfg.min_roe_pct > 0:
        res, roestats = KF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = KF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = KF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    funnel = {**kstats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<26} {v}")

    out = res if a.all else res[res["passes"]]
    cols = [c for c in KF.korea_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")  # Excel-safe Hangul

    hits = res[res["passes"]]
    print(f"\n--- {len(hits)} stock(s) >={cfg.discount_threshold:.0%} below "
          f"industry peers on >={cfg.min_metrics_passing} metrics ---")
    if not hits.empty:
        show = hits[["ticker", "name", "board", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "metrics_passing"]].head(30).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(lambda x: f"{x:.1%}")
        print(show.to_string(index=False))

    vu = res[res.get("pbr_bottom20_industry", False) & res["passes"]]
    if not vu.empty:
        print(f"\n{len(vu)} of these sit in the bottom 20% of their industry on "
              f"PBR - the KRX low-PBR disclosure criterion.")
    meta = {
        "asof": asof, "source": source, "board": a.board,
        # The exact invocation, so the dashboard shows a command that actually
        # reproduces this run rather than a guess assembled from thresholds.
        "cmd": "python main_kr.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "krw_per_usd": round(1.0 / krw_usd, 1),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard, sibling_boards
            build_dashboard(a.out, meta_path, a.dashboard,
                            boards=sibling_boards(a.board, a.dashboard))
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
