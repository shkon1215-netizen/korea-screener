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


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def current_ev_ebitda(row) -> float:
    """Today's EV/EBITDA on WiseReport's own definitions.

    Carry forward WiseReport's latest non-equity EV - net debt, minorities,
    whatever it counts - and move only the equity part by today's price:

        EV_now = EV_latest + (PBR_now - PBR_latest) x equity_latest

    PBR x equity is market value on the same share count BPS uses, so this
    stays on one provider's definitions end to end. It is also the trailing
    convention the history uses: each historical year is that year-end price
    over that year's filed EBITDA, and this is today's price over the latest.

    Consequence worth knowing: when earnings are surging, the latest filed
    EBITDA lags the price, and the stock reads expensive against its history
    until the next filing catches up. 삼성전자 in Sept 2026 is the example -
    price more than doubled on FY2025 EBITDA that predates the boom.
    """
    evx = _as_list(row.get("hist_evx"))
    pbr = _as_list(row.get("hist_pbr"))
    eq, eb = row.get("wr_equity"), row.get("wr_ebitda")
    pbr_now = row.get("price_to_book")
    try:
        evx_l, pbr_l = float(evx[-1]), float(pbr[-1])
        eq, eb, pbr_now = float(eq), float(eb), float(pbr_now)
    except (IndexError, TypeError, ValueError):
        return np.nan
    if not all(np.isfinite([evx_l, pbr_l, eq, eb, pbr_now])) or eb <= 0:
        return np.nan
    return (evx_l * eb + (pbr_now - pbr_l) * eq) / eb


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own five filed years - the third screen.

    The relative screen asks whether a name is cheap against its industry; the
    absolute screen whether it is cheap outright. This asks whether it is cheap
    against ITSELF, which catches what both miss: a company that has always
    traded at a premium and just de-rated, or one sitting in a sector that
    re-rated as a whole.

    Scored like the relative screen and held to the same rules. The benchmark
    is a median (invariant 4). A non-positive or out-of-bounds multiple is
    missing, never cheap - in history as well as today (invariant 2), so a
    loss year drops out of the benchmark rather than dragging it. EV/EBITDA is
    skipped for financials (invariant 6). Fewer than `hist_min_years` usable
    years means no benchmark: that is noise, not history. The ROE floor applies,
    as it does to the other two screens.
    """
    df = df.copy()
    fin = df.get("sector", pd.Series("", index=df.index)).fillna("") \
            .str.contains("Financial", case=False)
    df["evx_now"] = df.apply(current_ev_ebitda, axis=1)
    df.loc[fin, "evx_now"] = np.nan

    disc_cols = []
    for key, cur_col in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[cur_col]
        hist = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index))
        vals = hist.map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(lambda v, i=i: v[i] if i < len(v) else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
            cur = df["evx_now"]
        else:
            cur = pd.to_numeric(df.get(cur_col), errors="coerce")
        cur = cur.where((cur >= lo) & (cur <= hi))
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the ones that failed - the
    # same rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
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
    # Own five-year history: the median benchmark, today's discount to it,
    # and the five yearly values for the tooltip.
    cols += ["hist_years", "evx_now", "hist_n_valid", "hist_n_pass",
             "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
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
