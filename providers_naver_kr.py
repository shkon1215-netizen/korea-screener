"""Fallback Korea data layer: KIND (roster + 업종) + Naver (cross-section).

Why this exists
---------------
As of pykrx 1.2.x, data.krx.co.kr gates every *cross-sectional* endpoint
behind an account (KRX_ID / KRX_PW). Logged out it answers HTTP 400 with the
body 'LOGOUT', which pykrx surfaces as a KeyError on a Korean column name.
This module reaches the same numbers without an account:

  - roster + industry -> kind.krx.co.kr corpList.do  (KRX's own disclosure
    portal, not gated). Ships 업종, the KRX industry classification, which is
    a genuine upgrade over the yfinance `industry` the KRX path relies on.
  - price / market cap / PER / PBR / ROE / DPS -> finance.naver.com
    sise_market_sum, 50 names per page, ~20 pages per board.
  - trading calendar -> pykrx get_market_ohlcv, which runs off a different
    backend and keeps working while logged out.

Deliberate differences from KRXProvider, all of them visible in the output:

  1. The Naver cross-section is LIVE, not as-of a date. `date` arguments are
     accepted for interface compatibility and ignored; `asof` in the log is
     the last completed session, but the multiples are current.
  2. Naver publishes 거래량 (shares), not 거래대금 (value). Traded value is
     reconstructed as volume x close. Good enough for a liquidity gate,
     not a substitute for KRX's own figure.
  3. EPS and BPS are derived as close/PER and close/PBR rather than reported.
     That is an identity, and it keeps ROE consistent with the very multiples
     being screened - which is exactly the property CLAUDE.md wants from ROE.
     Naver's own reported ROE is carried alongside as `naver_roe_pct`.

Invariant 7 is preserved: everything here is cross-sectional and cheap. The
only per-ticker work is EV/EBITDA enrichment, which still runs last and only
on names that already cleared the size gate.
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
    def _set_fields(self, sosok: str) -> None:
        params = ([("menu", "market_sum"),
                   ("returnUrl", f"{NAVER_LIST}?sosok={sosok}")]
                  + [("fieldIds", f) for f in FIELD_IDS])
        self.s.get(NAVER_FIELDS, params=params,
                   headers={"Referer": NAVER_LIST}, timeout=40)

    def _board_pages(self, board: str) -> pd.DataFrame:
        from bs4 import BeautifulSoup
        sosok = BOARD_SOSOK[board]
        self._set_fields(sosok)

        rows, page = [], 1
        while page <= 80:
            r = self.s.get(NAVER_LIST, params={"sosok": sosok, "page": str(page)},
                           timeout=40)
            table = BeautifulSoup(r.text, "lxml").find("table", class_="type_2")
            if table is None:
                break
            heads = [th.get_text(strip=True) for th in table.find_all("th")]
            found = 0
            for tr in table.find_all("tr"):
                a = tr.find("a", href=re.compile(r"code=[0-9A-Z]{6}"))
                if a is None:
                    continue
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) != len(heads):
                    continue
                rec = dict(zip(heads, tds))
                rec["ticker"] = re.search(r"code=([0-9A-Z]{6})", a["href"]).group(1)
                rec["board"] = board
                rows.append(rec)
                found += 1
            if not found:
                break
            page += 1
            if page % 5 == 0:
                # The page loop is the slowest stretch of a run; serve.py turns
                # this into the progress line behind the Refresh button.
                log.info("%s: page %d, %d rows so far", board, page, len(rows))
            time.sleep(self.cfg.request_delay)

        log.info("%s: %d listings over %d pages", board, len(rows), page - 1)
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
        df = pd.concat(frames, ignore_index=True)

        out = pd.DataFrame({"ticker": df["ticker"], "board": df["board"]})
        out["name"] = df["종목명"].astype(str).str.strip()
        out["close_krw"] = df["현재가"].map(_cell)
        out["volume"] = df["거래량"].map(_cell)
        # Naver reports 시가총액 in 억원.
        out["market_cap_local"] = df["시가총액"].map(_cell) * 1e8
        out["trailing_pe"] = df["PER"].map(_cell)
        out["price_to_book"] = df["PBR"].map(_cell)
        out["naver_roe_pct"] = df["ROE"].map(_cell)
        dps = df["보통주배당금"].map(_cell)

        # Traded value is not published; reconstruct it. Flagged in the docstring
        # because it is a proxy, not KRX's 거래대금.
        out["value_krw"] = out["volume"] * out["close_krw"]
        out["shares_out"] = out["market_cap_local"] / out["close_krw"]
        out["div_yield"] = dps / out["close_krw"] * 100.0

        # EPS = P/PER and BPS = P/PBR are identities, so korea_filters' ROE
        # (EPS/BPS) stays consistent with the PER and PBR being screened on.
        out["trailing_eps"] = out["close_krw"] / out["trailing_pe"]
        out["book_value_ps"] = out["close_krw"] / out["price_to_book"]

        # Naver's board listing is every SECURITY on the board, so KOSPI comes
        # back ~2,500 lines: roughly 950 companies plus ~1,600 ETFs and ETNs.
        # Those carry no 업종 and would be dropped later by the industry gate,
        # but only after inflating every funnel count above it. Restrict to
        # lines whose common-stock code is a KIND-listed corporation. That
        # keeps preferred lines in the universe - deliberately, so invariant 1
        # is visibly doing work rather than silently moot.
        corps = set(self._kind_industry()["ticker"])
        if corps:
            keep = out["ticker"].map(K.common_line_of).isin(corps)
            log.info("universe: %d corporate lines kept, %d non-corporate "
                     "(ETF/ETN/fund) dropped", int(keep.sum()), int((~keep).sum()))
            out = out[keep].copy()

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
        return snap[["ticker", "board", "name"]].copy()

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
        cols = ["ticker", "trailing_pe", "price_to_book", "trailing_eps",
                "book_value_ps", "div_yield", "naver_roe_pct"]
        return snap[cols].copy()

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
