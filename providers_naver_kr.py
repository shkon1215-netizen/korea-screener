"""Fallback Korea data layer: KIND (roster + 업종) + Naver (cross-section).

Why this exists
---------------
As of pykrx 1.2.x, data.krx.co.kr gates every *cross-sectional* endpoint
behind an account (KRX_ID / KRX_PW). Logged out it answers HTTP 400 with the
body 'LOGOUT', which pykrx surfaces as a KeyError on a Korean column name.
This module reaches the same numbers without an account:

  - roster, price, market cap, traded value, halt status -> Naver's mobile
    API, m.stock.naver.com/api/stocks/marketValue/{board}, 100 per page.
  - industry -> kind.krx.co.kr corpList.do (KRX's own disclosure portal, not
    gated). Ships 업종, a genuine upgrade over the yfinance `industry` the KRX
    path relies on.
  - per-share fundamentals + 3y history -> m.stock.naver.com per ticker.
  - EBITDA -> yfinance (Naver publishes no depreciation line anywhere).
  - trading calendar -> pykrx get_market_ohlcv, which runs off a different
    backend and keeps working while logged out.

The HTML scrape of finance.naver.com/sise/sise_market_sum died in Sept 2026,
when Naver moved that page to a client-rendered app: the request still returns
200 with a full-looking document, but there is no <table> in it at all. The
JSON API that replaced it is better on every axis - exact KRW instead of
rounded 억원, real 거래대금 instead of a volume x close proxy, security type so
ETFs need no second source to identify, and per-row halt status.

Deliberate differences from KRXProvider, all of them visible in the output:

  1. The cross-section is LIVE, not as-of a date. `date` arguments are accepted
     for interface compatibility and ignored; `asof` in the log is the last
     completed session, but the prices are current.
  2. PER and PBR are struck against today's close over the latest reported EPS
     and BPS, not taken from Naver's own year-end figures. That keeps them
     consistent with the price the screen is actually looking at.

Invariant 7 is preserved: the size gate still runs on cheap cross-sectional
data. Per-ticker work - fundamentals, 3y history, EV/EBITDA - runs after it,
on survivors only.
"""
from __future__ import annotations

import io
import logging
import re
import time

import numpy as np
import pandas as pd
import requests

import config_kr as K
from config_kr import ScreenConfig
from providers_kr import DiskCache, _num

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

NAVER_MKT = "https://m.stock.naver.com/api/stocks/marketValue/{board}"
NAVER_LIST = "https://finance.naver.com/sise/sise_market_sum.naver"
NAVER_FIELDS = "https://finance.naver.com/sise/field_submit.naver"
KIND_LIST = "https://kind.krx.co.kr/corpgeneral/corpList.do"

# The six optional columns we ask Naver to render. Naver stores the choice on
# the session, so field_submit must be called before the first list page.
FIELD_IDS = ("market_sum", "per", "pbr", "roe", "dividend", "quant")

BOARD_SOSOK = {"KOSPI": "0", "KOSDAQ": "1"}
BOARD_KIND = {"KOSPI": "stockMkt", "KOSDAQ": "kosdaqMkt"}

# screener.sanitize_metrics suppresses EV/EBITDA wherever `sector` matches
# /Financial/ (invariant 6). KIND's 업종 is Korean, so map it across.
_FIN_TOKENS = ("은행", "금융", "보험", "증권", "저축", "신용", "여신", "자산운용")


KIND_ADMIN = "https://kind.krx.co.kr/investwarn/adminissue.do"


def fetch_admin_issue_names() -> set[str]:
    """관리종목 (administrative issue), from KIND. Works without an account.

    Two things to know before relying on this. KIND publishes the list by
    company NAME only - there is no 종목코드 anywhere in the markup - so
    matching is by exact name against the roster, and a renamed company slips
    through. And KIND's trading-halt (거래정지) list is not exposed at any
    endpoint that answers unauthenticated; only 관리종목 is covered here.

    In practice the list is almost entirely SPACs and micro-caps, so at a
    USD 600m size floor it removes nothing. It starts to matter as soon as
    --min-mcap comes down. Returns an empty set on any failure: this is a
    safety net, and it must never take the run down with it.
    """
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        r = s.post(KIND_ADMIN,
                   data={"method": "searchAdminIssueSub", "currentPageSize": "3000",
                         "pageIndex": "1", "orderMode": "1", "orderStat": "D",
                         "searchType": "13", "marketType": ""},
                   headers={"Referer": KIND_ADMIN + "?method=searchAdminIssueMain"},
                   timeout=60)
        r.raise_for_status()
        html = r.content.decode("utf-8", errors="replace")
        names = re.findall(r"alt='(?:코스닥|유가증권|코넥스)'>\s*([^<]+?)\s*</a>", html)
        out = {n.strip() for n in names if n.strip()}
        log.info("관리종목 list: %d names", len(out))
        return out
    except Exception as e:
        log.warning("관리종목 lookup failed (%s); continuing without it", e)
        return set()


NAVER_FIN = "https://m.stock.naver.com/api/stock/{code}/finance/annual"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1")

# Naver's row titles -> our short names. 당기순이익 is the headline figure and
# includes non-controlling interests; 지배주주순이익 sits beside it if you ever
# need the EPS-consistent basis instead.
FIN_ROWS = {"매출액": "rev", "영업이익": "op", "당기순이익": "np"}

# Latest-year per-share figures. PER and PBR are deliberately NOT taken from
# here: Naver's are struck at the fiscal year end, and the screen needs them
# against today's price. main_kr recomputes them as close/EPS and close/BPS.
FIN_LATEST = {"EPS": "trailing_eps", "BPS": "book_value_ps",
              "주당배당금": "dps", "ROE": "naver_roe_pct"}


def _fin_num(v) -> float:
    """Naver ships '3,336,059' in 억원, and '-' or '' for a period with no
    filing. Both of those are missing, and neither is zero."""
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "N/A", "None", "nan"):
        return np.nan
    return _num(s)


def cagr(values: list[float]) -> float:
    """Compound annual growth across the span the values actually cover.

    Undefined when the base is zero or negative - a company that lost money
    three years ago and makes money now has no meaningful growth *rate*, and
    inventing one (or flipping its sign) is worse than reporting nothing. The
    yearly figures are always shipped alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def fetch_financials(tickers: list[str], cache=None, delay: float = 0.12,
                     workers: int = 6, years: int = 3) -> pd.DataFrame:
    """Three years of revenue, operating profit, net profit and EBITDA.

    Revenue/operating/net come from Naver's mobile API, which flags analyst
    forecasts with `isConsensus: "Y"` - those are dropped, so only filed
    actuals are reported. CLAUDE.md's "no forward estimates" rule applies here
    as much as to the multiples.

    EBITDA is NOT in that payload and cannot be derived from it: Naver
    publishes no depreciation line. It comes from yfinance's income statement
    instead, whose operating income reconciles exactly with Naver's 영업이익
    (삼성전자 2025: 436,011억 in both), which is the check that says the two
    sources are describing the same company.

    Per-ticker and therefore slow, so call it LAST on survivors only -
    invariant 7.
    """
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sess = requests.Session()
    sess.headers.update({"User-Agent": MOBILE_UA,
                         "Referer": "https://m.stock.naver.com/"})

    def naver_part(code: str) -> dict:
        r = sess.get(NAVER_FIN.format(code=code), timeout=25)
        r.raise_for_status()
        info = (r.json() or {}).get("financeInfo") or {}
        actual = [t["key"] for t in info.get("trTitleList", [])
                  if t.get("isConsensus") != "Y"]
        actual = sorted(actual)[-years:]
        out = {"fin_years": ",".join(a[:4] for a in actual), "fin_n": len(actual)}
        newest = actual[-1] if actual else None
        for row in info.get("rowList", []):
            title = row.get("title")
            cols = row.get("columns", {})
            key = FIN_ROWS.get(title)
            if key:
                series = [_fin_num(cols.get(p, {}).get("value")) for p in actual]
                for i, v in enumerate(series, 1):
                    out[f"{key}_y{i}"] = v
                out[f"{key}_cagr"] = cagr(series)
            latest = FIN_LATEST.get(title)
            if latest and newest:
                out[latest] = _fin_num(cols.get(newest, {}).get("value"))
        return out

    def ebitda_part(code: str) -> dict:
        for suffix in (".KS", ".KQ"):
            try:
                stmt = yf.Ticker(f"{code}{suffix}").income_stmt
                if stmt is None or stmt.empty:
                    continue
                row = next((i for i in stmt.index if str(i) == "EBITDA"), None)
                if row is None:
                    row = next((i for i in stmt.index
                                if "Normalized EBITDA" in str(i)), None)
                if row is None:
                    continue
                # Newest column first; flip to oldest-first and convert to 억원
                # so it sits on the same scale as everything from Naver.
                cols = list(stmt.columns)[:years][::-1]
                series = [_num(stmt.loc[row, c]) / 1e8 for c in cols]
                out = {f"ebitda_y{i}": v for i, v in enumerate(series, 1)}
                out["ebitda_cagr"] = cagr(series)
                return out
            except Exception:
                continue
        return {}

    def one(code: str) -> dict:
        if cache is not None:
            hit = cache.get(f"fin_{code}")
            if hit:
                return {**hit, "ticker": code}
        rec = {"ticker": code}
        try:
            time.sleep(delay)
            rec.update(naver_part(code))
        except Exception as e:
            log.debug("financials failed for %s: %s", code, e)
        try:
            rec.update(ebitda_part(code))
        except Exception:
            pass
        if cache is not None:
            cache.set(f"fin_{code}", {k: (None if (isinstance(v, float) and not
                                                   np.isfinite(v)) else v)
                                      for k, v in rec.items() if k != "ticker"})
        return rec

    out, failed = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, t) for t in tickers]
        for i, f in enumerate(as_completed(futs), 1):
            rec = f.result()
            if not rec.get("fin_n"):
                failed += 1
            out.append(rec)
            if i % 50 == 0:
                log.info("financials %d/%d", i, len(tickers))
    if tickers and failed > len(tickers) * 0.3:
        # Naver answered 30/30 in testing, so a high failure rate means
        # throttling or a changed payload, not ordinary missing filings.
        log.warning("financials missing for %d of %d names - check for "
                    "rate limiting or an API change", failed, len(tickers))
    return pd.DataFrame(out)


def _fin_sector(industry: str) -> str:
    s = str(industry)
    return "Financial Services" if any(t in s for t in _FIN_TOKENS) else ""


def _cell(v) -> float:
    """Naver writes 'N/A' for a multiple it cannot compute, and blanks for
    suspended lines. Both are missing, neither is zero."""
    s = str(v).strip().replace(",", "")
    if not s or s in ("N/A", "-", "nan"):
        return np.nan
    return _num(s)


class NaverKindProvider:
    """Same surface as KRXProvider, no KRX account required."""

    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.cache = DiskCache(cfg.cache_dir, cfg.cache_ttl_hours)
        self._snap: pd.DataFrame | None = None
        self._industry: pd.DataFrame | None = None
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})

    # -- trading calendar ---------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """pykrx's per-ticker OHLCV endpoint is not behind the login wall, so
        a liquid name's own price history is a reliable session calendar."""
        from datetime import datetime, timedelta
        from pykrx import stock
        end = datetime.now()
        start = end - timedelta(days=int(n * 1.8) + 15)
        try:
            df = stock.get_market_ohlcv(start.strftime("%Y%m%d"),
                                        end.strftime("%Y%m%d"), "005930")
            days = [d.strftime("%Y%m%d") for d in df.index]
            if days:
                return days[-n:]
            log.warning("calendar came back empty; falling back to weekdays")
        except Exception as e:
            log.warning("calendar lookup failed (%s); falling back to weekdays", e)
        days = [(end - timedelta(days=i)).strftime("%Y%m%d")
                for i in range(int(n * 1.6))
                if (end - timedelta(days=i)).weekday() < 5]
        days.reverse()
        return days[-n:]

    # -- Naver cross-section -------------------------------------------
    def _board_pages(self, board: str) -> pd.DataFrame:
        """One JSON page per 100 names off Naver's mobile API.

        Replaced the old sise_market_sum HTML scrape in Sept 2026, when Naver
        moved that page to a client-rendered app and the table stopped existing
        in the HTML. The JSON is better than what it replaced: market cap and
        traded value arrive as exact KRW rather than rounded 억원, security type
        separates stocks from ETFs and ETNs without a second source, and each
        row carries its own trading-halt status.
        """
        rows, page = [], 1
        while page <= 60:
            r = self.s.get(NAVER_MKT.format(board=board),
                           params={"page": page, "pageSize": 100}, timeout=30)
            r.raise_for_status()
            d = r.json() or {}
            batch = d.get("stocks") or []
            if not batch:
                break
            for x in batch:
                halt = (x.get("tradeStopType") or {}).get("name", "")
                rows.append({
                    "ticker": x.get("itemCode", ""),
                    "board": board,
                    "name": (x.get("stockName") or "").strip(),
                    "kind": x.get("stockEndType", ""),
                    "halted": halt not in ("", "TRADING"),
                    "close_krw": _fin_num(x.get("closePriceRaw")),
                    "market_cap_local": _fin_num(x.get("marketValueRaw")),
                    "volume": _fin_num(x.get("accumulatedTradingVolumeRaw")),
                    "value_krw": _fin_num(x.get("accumulatedTradingValueRaw")),
                })
            if len(rows) >= int(d.get("totalCount") or 0):
                break
            page += 1
            if page % 5 == 0:
                # The slowest stretch of a run; serve.py turns this into the
                # progress line behind the Refresh button.
                log.info("%s: page %d, %d rows so far", board, page, len(rows))
            time.sleep(self.cfg.request_delay)

        log.info("%s: %d securities over %d pages", board, len(rows), page)
        return pd.DataFrame(rows)

    def _snapshot(self) -> pd.DataFrame:
        """Fetch once, serve roster + market cap + fundamentals from it."""
        if self._snap is not None:
            return self._snap

        frames = [self._board_pages(b) for b in self.cfg.boards
                  if b in BOARD_SOSOK]
        frames = [f for f in frames if not f.empty]
        if not frames:
            self._snap = pd.DataFrame()
            return self._snap
        out = pd.concat(frames, ignore_index=True)

        # The board listing is every SECURITY, so KOSPI arrives as ~2,500 lines:
        # ~945 companies plus ~1,530 ETFs and ETNs. The API labels them, so no
        # second source is needed to tell them apart. Preferred lines stay in
        # deliberately, so invariant 1 is visibly doing work rather than moot.
        before = len(out)
        out = out[out["kind"] == "stock"].copy()
        log.info("universe: %d stocks kept, %d ETF/ETN dropped",
                 len(out), before - len(out))

        out["shares_out"] = out["market_cap_local"] / out["close_krw"]

        self._snap = out
        return out

    # -- KIND roster: industry classification --------------------------
    def _kind_industry(self) -> pd.DataFrame:
        if self._industry is not None:
            return self._industry
        frames = []
        for board in self.cfg.boards:
            if board not in BOARD_KIND:
                continue
            try:
                r = self.s.get(KIND_LIST,
                               params={"method": "download", "searchType": "13",
                                       "marketType": BOARD_KIND[board]},
                               timeout=60)
                r.raise_for_status()
                html = r.content.decode("euc-kr", errors="replace")
                t = pd.read_html(io.StringIO(html),
                                 converters={"종목코드": str})[0]
                t = t[["종목코드", "업종"]].copy()
                t["ticker"] = (t["종목코드"].astype(str).str.strip()
                               .str.upper().str.zfill(6))
                frames.append(t[["ticker", "업종"]])
                log.info("KIND %s: %d industry rows", board, len(t))
            except Exception as e:
                log.error("KIND industry fetch failed for %s: %s", board, e)
        if not frames:
            self._industry = pd.DataFrame(columns=["ticker", "industry", "sector"])
            return self._industry
        df = pd.concat(frames, ignore_index=True).drop_duplicates("ticker")
        df["industry"] = df["업종"].astype(str).str.strip()
        df["sector"] = df["industry"].map(_fin_sector)
        self._industry = df[["ticker", "industry", "sector"]]
        return self._industry

    # -- KRXProvider-compatible surface --------------------------------
    def listing_roster(self, date: str) -> pd.DataFrame:
        snap = self._snapshot()
        if snap.empty:
            return pd.DataFrame()
        return snap[["ticker", "board", "name", "halted"]].copy()

    def market_cap_snapshot(self, date: str) -> pd.DataFrame:
        snap = self._snapshot()
        if snap.empty:
            return pd.DataFrame()
        return snap[["ticker", "close_krw", "market_cap_local", "volume",
                     "value_krw", "shares_out"]].copy()

    def fundamentals_snapshot(self, date: str) -> pd.DataFrame:
        snap = self._snapshot()
        if snap.empty:
            return pd.DataFrame()
        # Per-share fundamentals are no longer cross-sectional: the market-value
        # API carries prices and sizes but no PER/PBR/EPS/BPS. Those now arrive
        # per ticker from fetch_financials, AFTER the size gate, which keeps
        # invariant 7 intact - the gate still runs on cheap cross-sectional data.
        return pd.DataFrame(columns=["ticker"])

    def liquidity(self, dates: list[str]) -> pd.DataFrame:
        """Naver's cross-section is a live snapshot with no history, so a
        60-session median is not available cheaply here. Returns today's
        traded value as a single observation; n_obs=1 makes that explicit.
        Prefer --skip-liquidity on this source, or the KRX path if you need
        a real ADV."""
        snap = self._snapshot()
        if snap.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "n_obs"])
        log.warning("liquidity on the naver source is a 1-session snapshot, "
                    "not a %d-session median", len(dates))
        return pd.DataFrame({"ticker": snap["ticker"],
                             "adv_local": snap["value_krw"], "n_obs": 1})

    def krw_to_usd(self) -> float:
        cached = self.cache.get("fx_krw")
        if cached:
            return float(cached)
        try:
            import yfinance as yf
            h = yf.Ticker("KRW=X").history(period="5d")
            if not h.empty:
                rate = 1.0 / float(h["Close"].dropna().iloc[-1])
                self.cache.set("fx_krw", rate)
                return rate
        except Exception as e:
            log.warning("live FX lookup failed (%s)", e)
        log.warning("falling back to hardcoded KRW rate - pass --fx to override")
        return 1.0 / 1386.0

    def enrich(self, tickers: list[str]) -> pd.DataFrame:
        """Industry already came from KIND, so the only per-ticker work left is
        EV/EBITDA. Still runs last, still only on names past the size gate."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import yfinance as yf

        ind = self._kind_industry()

        def one(t):
            cached = self.cache.get(f"ev_{t}")
            if cached is not None:
                return {"ticker": t, "ev_to_ebitda": _num(cached.get("ev"))}
            ev = np.nan
            for suffix in (".KS", ".KQ"):
                try:
                    time.sleep(self.cfg.request_delay)
                    info = yf.Ticker(f"{t}{suffix}").info or {}
                    if info.get("sector") or info.get("industry"):
                        ev = _num(info.get("enterpriseToEbitda"))
                        break
                except Exception:
                    continue
            self.cache.set(f"ev_{t}", {"ev": None if pd.isna(ev) else ev})
            return {"ticker": t, "ev_to_ebitda": ev}

        out = []
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            futs = [ex.submit(one, t) for t in tickers]
            for i, f in enumerate(as_completed(futs), 1):
                out.append(f.result())
                if i % 50 == 0:
                    log.info("EV/EBITDA %d/%d", i, len(tickers))
        ev = pd.DataFrame(out)
        return ev.merge(ind, on="ticker", how="left")
