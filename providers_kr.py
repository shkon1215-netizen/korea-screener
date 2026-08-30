"""Korea data layer.

Primary source is pykrx, which reads KRX's own published numbers: full
listing rosters, market cap, traded value, and PER/PBR/EPS/BPS/DIV. That is
both free and authoritative, and it removes the universe problem that limits
the global version of this tool.

Two things KRX does not publish in a convenient form:
  - industry classification -> enriched from yfinance, or supply your own
  - EV/EBITDA               -> enriched from yfinance; often missing

Order of operations matters. Size and liquidity gates run on cheap
cross-sectional pykrx calls FIRST, so the slow per-ticker yfinance enrichment
only ever touches the few hundred names that already cleared them.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from config_kr import ScreenConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
def _call(module, names: tuple[str, ...], *args, **kwargs):
    """pykrx renamed several functions across versions and added dispatchers.
    Try each known spelling so this survives a version bump."""
    last = None
    for n in names:
        fn = getattr(module, n, None)
        if fn is None:
            continue
        try:
            return fn(*args, **kwargs)
        except TypeError as e:
            last = e
            continue
    raise AttributeError(f"none of {names} worked on pykrx.stock ({last})")


class DiskCache:
    def __init__(self, path: str, ttl_hours: int):
        self.path, self.ttl = path, timedelta(hours=ttl_hours)
        os.makedirs(path, exist_ok=True)

    def _f(self, key):
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return os.path.join(self.path, f"{safe}.json")

    def get(self, key):
        f = self._f(key)
        if not os.path.exists(f):
            return None
        if datetime.now() - datetime.fromtimestamp(os.path.getmtime(f)) > self.ttl:
            return None
        try:
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def set(self, key, value):
        try:
            with open(self._f(key), "w", encoding="utf-8") as fh:
                json.dump(value, fh, ensure_ascii=False)
        except Exception as e:
            log.debug("cache write failed %s: %s", key, e)


# ---------------------------------------------------------------------------
class KRXProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.cache = DiskCache(cfg.cache_dir, cfg.cache_ttl_hours)
        try:
            from pykrx import stock  # noqa: F401
        except ImportError as e:
            raise ImportError("pip install pykrx") from e

    # -- trading calendar ---------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        from pykrx import stock
        end = datetime.now()
        start = end - timedelta(days=int(n * 1.8) + 15)
        try:
            df = stock.get_index_ohlcv(start.strftime("%Y%m%d"),
                                       end.strftime("%Y%m%d"), "1001")  # KOSPI
            days = [d.strftime("%Y%m%d") for d in df.index]
        except Exception as e:
            log.warning("calendar lookup failed (%s); falling back to weekdays", e)
            days = [(end - timedelta(days=i)).strftime("%Y%m%d")
                    for i in range(int(n * 1.6)) if (end - timedelta(days=i)).weekday() < 5]
            days.reverse()
        return days[-n:]

    # -- universe ------------------------------------------------------
    def listing_roster(self, date: str) -> pd.DataFrame:
        from pykrx import stock
        rows = []
        for board in self.cfg.boards:
            try:
                tickers = _call(stock, ("get_market_ticker_list",), date, market=board)
            except Exception as e:
                log.error("ticker list failed for %s: %s", board, e)
                continue
            for t in tickers:
                rows.append({"ticker": t, "board": board})
            log.info("%s: %d listings", board, len(tickers))

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        names = {}
        for t in df["ticker"]:
            c = self.cache.get(f"name_{t}")
            if c:
                names[t] = c
        missing = [t for t in df["ticker"] if t not in names]
        if missing:
            log.info("resolving %d company names...", len(missing))
            with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
                futs = {ex.submit(self._name_of, t): t for t in missing}
                for fut in as_completed(futs):
                    t = futs[fut]
                    names[t] = fut.result()
        df["name"] = df["ticker"].map(names).fillna("")
        return df

    def _name_of(self, ticker: str) -> str:
        from pykrx import stock
        try:
            time.sleep(self.cfg.request_delay)
            n = stock.get_market_ticker_name(ticker) or ""
            if n:
                self.cache.set(f"name_{ticker}", n)
            return n
        except Exception:
            return ""

    # -- cross-sectional market cap & fundamentals ---------------------
    def market_cap_snapshot(self, date: str) -> pd.DataFrame:
        from pykrx import stock
        frames = []
        for board in self.cfg.boards:
            try:
                d = _call(stock, ("get_market_cap", "get_market_cap_by_ticker"),
                          date, market=board)
                d = d.reset_index()
                d.columns = [str(c) for c in d.columns]
                frames.append(d)
            except Exception as e:
                log.error("market cap snapshot failed %s %s: %s", board, date, e)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return df.rename(columns={
            "티커": "ticker", "종가": "close_krw", "시가총액": "market_cap_local",
            "거래량": "volume", "거래대금": "value_krw", "상장주식수": "shares_out",
        })

    def fundamentals_snapshot(self, date: str) -> pd.DataFrame:
        from pykrx import stock
        frames = []
        for board in self.cfg.boards:
            try:
                d = _call(stock,
                          ("get_market_fundamental", "get_market_fundamental_by_ticker"),
                          date, market=board)
                d = d.reset_index()
                d.columns = [str(c) for c in d.columns]
                frames.append(d)
            except Exception as e:
                log.error("fundamentals snapshot failed %s: %s", board, e)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df = df.rename(columns={
            "티커": "ticker", "BPS": "book_value_ps", "PER": "trailing_pe",
            "PBR": "price_to_book", "EPS": "trailing_eps",
            "DIV": "div_yield", "DPS": "dps",
        })
        for c in ("trailing_pe", "price_to_book", "trailing_eps",
                  "book_value_ps", "div_yield", "dps"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    # -- liquidity: median daily traded value over N sessions ----------
    def liquidity(self, dates: list[str]) -> pd.DataFrame:
        """One cross-sectional call per date covers the whole market, so ~60
        calls give ADV for all ~2,600 listings."""
        series = {}
        for i, d in enumerate(dates, 1):
            snap = self.market_cap_snapshot(d)
            if snap.empty or "value_krw" not in snap.columns:
                continue
            series[d] = snap.set_index("ticker")["value_krw"]
            if i % 10 == 0:
                log.info("liquidity %d/%d sessions", i, len(dates))
            time.sleep(self.cfg.request_delay)
        if not series:
            return pd.DataFrame(columns=["ticker", "adv_local", "n_obs"])
        wide = pd.DataFrame(series)
        return pd.DataFrame({
            "ticker": wide.index,
            "adv_local": wide.median(axis=1, skipna=True).values,
            "n_obs": wide.notna().sum(axis=1).values,
        })

    # -- FX -------------------------------------------------------------
    def krw_to_usd(self) -> float:
        cached = self.cache.get("fx_krw")
        if cached:
            return float(cached)
        try:
            import yfinance as yf
            h = yf.Ticker("KRW=X").history(period="5d")   # USD per KRW is 1/this
            if not h.empty:
                rate = 1.0 / float(h["Close"].dropna().iloc[-1])
                self.cache.set("fx_krw", rate)
                return rate
        except Exception as e:
            log.warning("live FX lookup failed (%s)", e)
        log.warning("falling back to hardcoded KRW rate - pass --fx to override")
        return 1.0 / 1386.0

    # -- enrichment: industry + EV/EBITDA ------------------------------
    def enrich(self, tickers: list[str]) -> pd.DataFrame:
        """Per-ticker yfinance lookup. Call this LAST, on survivors only."""
        import yfinance as yf

        def one(t):
            cached = self.cache.get(f"yf_{t}")
            if cached:
                return cached
            rec = {"ticker": t, "sector": "", "industry": "",
                   "ev_to_ebitda": np.nan, "yf_symbol": ""}
            for suffix in (".KS", ".KQ"):
                try:
                    time.sleep(self.cfg.request_delay)
                    info = yf.Ticker(f"{t}{suffix}").info or {}
                    if info.get("sector") or info.get("industry"):
                        rec.update(
                            sector=info.get("sector") or "",
                            industry=info.get("industry") or "",
                            ev_to_ebitda=_num(info.get("enterpriseToEbitda")),
                            yf_symbol=f"{t}{suffix}")
                        break
                except Exception:
                    continue
            self.cache.set(f"yf_{t}", rec)
            return rec

        out = []
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            futs = [ex.submit(one, t) for t in tickers]
            for i, f in enumerate(as_completed(futs), 1):
                out.append(f.result())
                if i % 50 == 0:
                    log.info("enrichment %d/%d", i, len(tickers))
        return pd.DataFrame(out)


def _num(v) -> float:
    try:
        f = float(v)
        return f if np.isfinite(f) else np.nan
    except (TypeError, ValueError):
        return np.nan
