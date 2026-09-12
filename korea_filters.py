"""Korea-specific filters layered on top of the generic screener."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_kr as K

log = logging.getLogger(__name__)


def tag_share_classes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["is_preferred"] = df["ticker"].map(K.is_preferred)
    df["common_line"] = df["ticker"].map(K.common_line_of)
    df["is_spac"] = df["name"].map(K.is_spac)
    df["is_reit"] = df["name"].map(K.is_reit)
    df["is_holdco"] = df["name"].map(K.is_holdco)
    return df


def apply_korea_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Remove share classes and vehicles whose cheapness is structural.

    Every exclusion here removes something a naive screener would rank at or
    near the top. Preferred lines are the big one: 005935 (Samsung Electronics
    preferred) has traded persistently below 005930 for years because it
    carries no vote, not because the business got cheaper. Screening both
    against the same industry cohort flags the preferred every single run.
    """
    df = tag_share_classes(df)
    stats = {"listings": len(df)}

    if cfg.exclude_preferred:
        n = int(df["is_preferred"].sum())
        df = df[~df["is_preferred"]]
        stats["dropped_preferred"] = n
    if cfg.exclude_spac:
        n = int(df["is_spac"].sum())
        df = df[~df["is_spac"]]
        stats["dropped_spac"] = n
    if cfg.exclude_reits:
        n = int(df["is_reit"].sum())
        df = df[~df["is_reit"]]
        stats["dropped_reit"] = n
    if cfg.exclude_holdcos:
        n = int(df["is_holdco"].sum())
        df = df[~df["is_holdco"]]
        stats["dropped_holdco"] = n
    else:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())

    stats["after_korea_filters"] = len(df)
    return df.copy(), stats


def apply_halt_filter(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop 거래정지 (suspended) lines.

    Known gap #3 was open for as long as no free source published halt status.
    Naver's market-value API carries `tradeStopType` per row, so it closes for
    free - and unlike the 관리종목 list it matches on ticker, not company name.
    A suspended line has a stale last price, which would otherwise be screened
    as if it were live and could look arbitrarily cheap.
    """
    if "halted" not in df.columns:
        return df, {}
    df = df.copy()
    hit = df["halted"].fillna(False).astype(bool)
    n = int(hit.sum())
    if n:
        log.info("거래정지 removed: %s", ", ".join(df.loc[hit, "name"].head(10)))
    return df[~hit].copy(), {"dropped_trading_halt": n}


def apply_admin_issue_filter(df: pd.DataFrame, admin_names: set) -> tuple[pd.DataFrame, dict]:
    """Drop 관리종목 by name. Runs on the roster, before the size gate, so the
    funnel shows it and so it still protects when --min-mcap is lowered."""
    if not admin_names:
        return df, {"dropped_admin_issue": 0}
    df = df.copy()
    hit = df["name"].astype(str).str.strip().isin(admin_names)
    n = int(hit.sum())
    if n:
        log.info("관리종목 removed: %s", ", ".join(df.loc[hit, "name"].head(10)))
    return df[~hit].copy(), {"dropped_admin_issue": n}


def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Flag the KRX low-PBR disclosure criterion.

    KRX has been building a regime that publicly identifies listed firms whose
    PBR sits in the bottom 20% of their industry across consecutive reporting
    periods, with firms that file improvement plans able to avoid disclosure.
    Whatever one thinks of the policy, it creates a live catalyst attached to
    a screen that is nearly identical to this one, so it is worth surfacing.

    Caveat: this computes the CURRENT cross-section only. The actual criterion
    requires the condition to hold across two consecutive periods, which needs
    a history you would have to accumulate by saving dated runs.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """Cheap context that helps separate value from value trap.

    ROE is derived from KRX's own EPS and BPS rather than a vendor field, so
    it is consistent with the PER and PBR being screened on. A stock cheap on
    PBR with high ROE is a different proposition from one cheap on PBR because
    it earns nothing - the latter is usually cheap for cause.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require the screen's survivors to actually earn something.

    Runs AFTER add_quality_context, because it needs roe_pct, and after
    scoring, because it narrows `passes` rather than replacing it. Nothing
    about avg_discount changes - invariant 5 still averages every metric with
    data. This only decides who is worth reading.

    Missing ROE fails the gate. A company whose EPS or BPS could not be
    resolved is not a company we can call profitable.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    PBR below 1 means the market values the company at less than its stated
    book. EV/EBITDA below 8 means the whole enterprise is priced at under
    eight years of operating cash earnings. Neither asks what the neighbours
    trade at, which is the point: invariant 3 keeps peer groups honest, but a
    peer group where everything is expensive still produces "cheap" names, and
    one where everything is cheap hides them.

    The two tests do not cover the same universe. Invariant 6 suppresses
    EV/EBITDA for financials, so a bank or insurer has no EV/EBITDA to test
    and can never clear a strict both-metrics rule - which would silently
    exclude exactly the corner of KOSPI that trades furthest below book.
    `abs_financials_pbr_only` lets those names qualify on PBR alone; it is off
    by default so the strict reading is what you get unless you ask for it.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = df.get("sector", pd.Series("", index=df.index)).fillna("") \
            .str.contains("Financial", case=False)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    # Fair PBR implied by the return on equity against a required return.
    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    # Missing yield fails: unknown is not the same as paid.
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        # Financials and 기타 금융업 holdcos have no EV/EBITDA to test, so a
        # strict both-metrics rule would exclude every one of them - which is
        # exactly the corner of KOSPI trading furthest below book.
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_div_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def korea_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "name", "board", "sector", "industry",
            "market_cap_usd", "adv_usd", "close_krw"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank"]
    # Three-year history: yearly figures in 억원, oldest first, plus the
    # compound rate across the span they cover.
    cols += ["fin_years", "fin_n"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry", "is_holdco",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
