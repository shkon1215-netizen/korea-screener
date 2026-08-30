"""Configuration for the Korea (KRX) valuation screener."""
from __future__ import annotations

from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

METRIC_LABELS = {
    "trailing_pe": "PER",
    "price_to_book": "PBR",
    "ev_to_ebitda": "EV/EBITDA",
}

# KRX reports PER/PBR as 0 for loss-making or negative-equity firms.
# The lower bound turns those into "missing", never into "cheap".
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000     # ~KRW 832bn at 1,386
    min_adv_usd: float = 4_000_000              # ~KRW 5.5bn
    adv_lookback_days: int = 60                 # trading days

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    # A low multiple on a company that does not earn its cost of capital is
    # arithmetic, not a mispricing - CLAUDE.md's own interpretation note. The
    # floor makes that explicit instead of leaving it to the reader.
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0      # "double digit or closer" - highlight tier

    # --- Absolute value screen ---
    # The relative screen asks "cheap versus its peers". That misses a cohort
    # that is cheap in absolute terms while sitting in an industry where
    # everything is cheap - which in Korea is most of the market. This is the
    # second, independent test; a name can pass either or both.
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True          # reuse min_roe_pct as the quality floor
    abs_financials_pbr_only: bool = True  # see korea_filters.apply_absolute_screen

    # Fair PBR ~ ROE / cost of equity. A company earning 12% against a 10%
    # required return is worth more than book, so 0.5x book is a real gap
    # rather than merely a small number. This is the only test here that says
    # WHY a low PBR is wrong instead of just noting that it is low.
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0

    # Cash actually returned - the core of the Value-Up push. A missing yield
    # fails: unknown is not the same as paid.
    abs_min_div_yield: float = 2.0        # 0 disables

    # 관리종목. KIND publishes the list free, but by company NAME only.
    exclude_admin_issue: bool = True

    # --- Peer groups ---
    # Korea-only holds the country effect constant, so industry-alone peers
    # are legitimate here. The freed dimension goes to the listing board:
    # KOSDAQ growth names carry structurally richer multiples than KOSPI.
    peer_keys: tuple[str, ...] = ("industry", "board")
    fallback_peer_keys: tuple[str, ...] = ("industry",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- Korea-specific universe hygiene ---
    exclude_preferred: bool = True      # 우선주: permanent structural discount
    exclude_spac: bool = True           # 스팩: shell companies, no operations
    exclude_reits: bool = True          # 리츠: NAV-based, not earnings-based
    flag_holdcos: bool = True           # 지주/홀딩스: permanent NAV discount
    exclude_holdcos: bool = False       # flagged by default, not dropped

    exclude_sectors: tuple[str, ...] = ()
    boards: tuple[str, ...] = ("KOSPI", "KOSDAQ")

    # --- Fetching ---
    max_workers: int = 6
    request_delay: float = 0.12
    cache_dir: str = ".kr_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Share-class and vehicle detection
# ---------------------------------------------------------------------------
# KRX 6-digit codes: common stock ends in '0'. Preferred lines reuse the first
# five digits with a different final digit (5 = old-style 구형우선주,
# 7/9 = new-style 신형우선주). Samsung Electronics: 005930 common,
# 005935 preferred - which has traded ~20% below the common for years for
# reasons that have nothing to do with the business being cheap.
def is_preferred(ticker: str) -> bool:
    t = str(ticker).strip()
    return len(t) == 6 and t[-1] != "0"


def common_line_of(ticker: str) -> str:
    """The common-stock code a preferred line belongs to."""
    t = str(ticker).strip()
    return t[:5] + "0" if len(t) == 6 else t


SPAC_TOKENS = ("스팩", "SPAC")
REIT_TOKENS = ("리츠", "REIT")
HOLDCO_TOKENS = ("지주", "홀딩스", "HOLDINGS", "HOLDCO")


def _has(name: str, tokens) -> bool:
    n = str(name).upper()
    return any(tok.upper() in n for tok in tokens)


def is_spac(name: str) -> bool:
    return _has(name, SPAC_TOKENS)


def is_reit(name: str) -> bool:
    return _has(name, REIT_TOKENS)


def is_holdco(name: str) -> bool:
    return _has(name, HOLDCO_TOKENS)
