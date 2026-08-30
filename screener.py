"""Screening engine (Korea build).

sanitize_metrics, compute_peer_benchmarks and score are byte-identical to the
global version - the relative-valuation maths does not care which market it
is pointed at. Only the gates below differ, because currency and country are
constants in a Korea-only universe.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config_kr import METRIC_BOUNDS, METRIC_LABELS, ScreenConfig

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 1. Clean metrics
# --------------------------------------------------------------------------
def sanitize_metrics(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    """Null out invalid multiples.

    This is the single most important step. A negative P/E means the company
    lost money - it is NOT a cheap stock, and letting it through would make
    every loss-maker look like a screaming buy. Same for negative book value
    and negative EBITDA.
    """
    df = df.copy()
    for m in cfg.metrics:
        lo, hi = METRIC_BOUNDS[m]
        v = pd.to_numeric(df.get(m), errors="coerce")
        df[m] = v.where((v > 0) & (v >= lo) & (v <= hi))

    # EV/EBITDA is meaningless for banks and insurers (no meaningful EV).
    if "ev_to_ebitda" in df.columns and "sector" in df.columns:
        fin = df["sector"].fillna("").str.contains("Financial", case=False)
        df.loc[fin, "ev_to_ebitda"] = np.nan
    return df


# --------------------------------------------------------------------------
# 2. Size & liquidity gates
# --------------------------------------------------------------------------
def apply_usd_conversion(df: pd.DataFrame, krw_usd: float) -> pd.DataFrame:
    """One scalar rate for the whole market - a real simplification versus the
    global build, which had to resolve and validate ~30 currencies."""
    df = df.copy()
    df["fx_to_usd"] = krw_usd
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * krw_usd
    if "adv_local" in df.columns:
        df["adv_usd"] = pd.to_numeric(df["adv_local"], errors="coerce") * krw_usd
    return df


def apply_gates(df: pd.DataFrame, cfg: ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Size, liquidity and classification gates. No country filter needed."""
    stats: dict[str, int] = {"start": len(df)}
    d = df

    if cfg.exclude_sectors:
        d = d[~d["sector"].isin(cfg.exclude_sectors)]

    d = d[d["market_cap_usd"].notna() & (d["market_cap_usd"] >= cfg.min_market_cap_usd)]
    stats["after_market_cap"] = len(d)

    if "adv_usd" in d.columns and cfg.min_adv_usd > 0:
        d = d[d["adv_usd"].notna() & (d["adv_usd"] >= cfg.min_adv_usd)]
    stats["after_liquidity"] = len(d)

    d = d[d["industry"].fillna("").ne("")]
    stats["after_industry_known"] = len(d)
    return d.copy(), stats


# --------------------------------------------------------------------------
# 3. Peer benchmarks
# --------------------------------------------------------------------------
def _winsorized_median(s: pd.Series, pct: float) -> float:
    s = s.dropna()
    if s.empty:
        return np.nan
    if pct > 0 and len(s) >= 10:
        lo, hi = s.quantile(pct), s.quantile(1 - pct)
        s = s.clip(lo, hi)
    return float(s.median())


def compute_peer_benchmarks(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    """Attach peer median + peer count + percentile rank for each metric.

    Peer groups are built on cfg.peer_keys (default industry x region). If a
    group has fewer than min_peers valid observations for a metric, we fall
    back to cfg.fallback_peer_keys (industry only). If that is still too thin,
    the metric is left unscored rather than benchmarked against noise.
    """
    df = df.copy()

    for m in cfg.metrics:
        med = pd.Series(np.nan, index=df.index, dtype=float)
        cnt = pd.Series(0, index=df.index, dtype=int)
        basis = pd.Series("", index=df.index, dtype=object)
        rank = pd.Series(np.nan, index=df.index, dtype=float)

        for keys, label in ((list(cfg.peer_keys), "x".join(cfg.peer_keys)),
                            (list(cfg.fallback_peer_keys), "x".join(cfg.fallback_peer_keys))):
            need = med.isna()
            if not need.any():
                break
            g = df.groupby(keys, dropna=False)[m]
            # Peer count excludes the stock itself so a lone name can't
            # benchmark against its own multiple.
            valid = df[m].notna()
            grp_n = g.transform("count")
            self_n = valid.astype(int)
            peer_n = (grp_n - self_n).clip(lower=0)

            grp_med = g.transform(lambda s: _winsorized_median(s, cfg.winsor_pct))
            grp_rank = g.rank(pct=True)

            ok = need & (peer_n >= cfg.min_peers) & grp_med.notna() & (grp_med > 0)
            med[ok] = grp_med[ok]
            cnt[ok] = peer_n[ok]
            rank[ok] = grp_rank[ok]
            basis[ok] = label

        df[f"{m}_peer_median"] = med
        df[f"{m}_peer_n"] = cnt
        df[f"{m}_peer_basis"] = basis
        df[f"{m}_pct_rank"] = rank
        # Positive discount = cheaper than peers.
        df[f"{m}_discount"] = (med - df[m]) / med
    return df


# --------------------------------------------------------------------------
# 4. Score & rank
# --------------------------------------------------------------------------
def score(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    df = df.copy()
    disc_cols = [f"{m}_discount" for m in cfg.metrics]

    df["n_valid_metrics"] = df[disc_cols].notna().sum(axis=1)
    df["n_metrics_passing"] = (df[disc_cols] >= cfg.discount_threshold).sum(axis=1)
    df["metrics_passing"] = df.apply(
        lambda r: ", ".join(
            METRIC_LABELS[m] for m in cfg.metrics
            if pd.notna(r[f"{m}_discount"]) and r[f"{m}_discount"] >= cfg.discount_threshold
        ), axis=1)

    # Headline score: mean discount across metrics that have data. Averaging
    # only the passing ones would reward a stock that is 40% cheap on P/B and
    # 30% expensive on P/E.
    df["avg_discount"] = df[disc_cols].mean(axis=1, skipna=True)
    df["median_pct_rank"] = df[[f"{m}_pct_rank" for m in cfg.metrics]].median(axis=1, skipna=True)

    df["passes"] = (
        (df["n_valid_metrics"] >= cfg.min_valid_metrics)
        & (df["n_metrics_passing"] >= cfg.min_metrics_passing)
    )
    return df.sort_values(["passes", "avg_discount"], ascending=[False, False])


def run_screen(df: pd.DataFrame, krw_usd: float, cfg: ScreenConfig):
    df = sanitize_metrics(df, cfg)
    df = apply_usd_conversion(df, krw_usd)
    df, stats = apply_gates(df, cfg)
    if df.empty:
        return df, stats
    df = compute_peer_benchmarks(df, cfg)
    df = score(df, cfg)
    stats["passing"] = int(df["passes"].sum())
    return df, stats


def output_columns(cfg: ScreenConfig) -> list[str]:
    cols = ["ticker", "name", "board", "sector", "industry",
            "market_cap_usd", "adv_usd"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount", f"{m}_peer_n", f"{m}_pct_rank"]
    cols += ["n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
