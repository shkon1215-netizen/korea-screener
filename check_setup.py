"""Pre-flight check for the Korea screener.

Tests every external call the screener depends on, one at a time, and prints
exactly what came back. Run this BEFORE main_kr.py - a full run takes 10-20
minutes and it is miserable to discover a column-name mismatch at minute 18.

Run: python check_setup.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

results: list[tuple[str, bool, str]] = []


def check(label: str):
    def deco(fn):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        results.append((label, ok, msg))
        print(f"[{'OK  ' if ok else 'FAIL'}] {label}\n       {msg}\n")
        return fn
    return deco


print("=" * 68)
print("Korea screener pre-flight check")
print("=" * 68 + "\n")

# ---------------------------------------------------------------- imports
@check("imports and versions")
def _imports():
    import pandas as pd
    import numpy as np
    msgs = [f"python {sys.version.split()[0]}",
            f"pandas {pd.__version__}", f"numpy {np.__version__}"]
    try:
        import pykrx
        msgs.append(f"pykrx {getattr(pykrx, '__version__', 'unknown')}")
    except ImportError:
        return False, "pykrx NOT installed -> pip install pykrx"
    try:
        import yfinance as yf
        msgs.append(f"yfinance {yf.__version__}")
    except ImportError:
        return False, "yfinance NOT installed -> pip install yfinance"
    warn = ""
    if int(pd.__version__.split(".")[0]) >= 3:
        warn = ("\n       WARNING: pandas 3.x may break pykrx. If later checks "
                "fail oddly,\n       try: pip install 'pandas<3'")
    return True, " | ".join(msgs) + warn


# ------------------------------------------------------------- credentials
@check("KRX credentials (KRX_ID / KRX_PW)")
def _creds():
    """As of pykrx 1.2.x, data.krx.co.kr gates every CROSS-SECTIONAL endpoint
    behind a logged-in account: the roster, the market-cap and fundamentals
    snapshots, and the index calendar. Logged out, the portal answers HTTP 400
    with the body 'LOGOUT'; pykrx tries to parse that as JSON, fails, and the
    error surfaces downstream as a KeyError on a Korean column name ('지수명',
    '시장'). That looks exactly like a column-rename problem and is not one.
    Check credentials first so nobody chases the wrong bug again.

    Per-ticker calls (get_market_ohlcv, get_market_ticker_name) run off a
    different backend and keep working while logged out, which is why the
    failure looks partial rather than total.
    """
    import os
    uid, pw = os.getenv("KRX_ID"), os.getenv("KRX_PW")
    if not uid or not pw:
        missing = " and ".join(n for n, v in (("KRX_ID", uid), ("KRX_PW", pw)) if not v)
        return False, (
            missing + " not set -> every cross-sectional KRX call will fail.\n"
            "       Register free at https://data.krx.co.kr, then set them:\n"
            "         PowerShell:  $env:KRX_ID='you'; $env:KRX_PW='secret'\n"
            "         bash:        export KRX_ID=you KRX_PW=secret")
    return True, "KRX_ID set (" + uid[:2] + "***), KRX_PW set   (values not printed)"


# ---------------------------------------------------------------- calendar
TRADING_DATE = None


@check("trading calendar (KOSPI index history)")
def _calendar():
    global TRADING_DATE
    from pykrx import stock
    end = datetime.now()
    start = end - timedelta(days=20)
    df = stock.get_index_ohlcv(start.strftime("%Y%m%d"),
                               end.strftime("%Y%m%d"), "1001")
    if df is None or df.empty:
        return False, ("no index data returned - network blocked, KRX down, or "
                       "not logged in - see the KRX credentials check above")
    TRADING_DATE = df.index[-1].strftime("%Y%m%d")
    return True, f"{len(df)} sessions; most recent trading day = {TRADING_DATE}"


# ---------------------------------------------------------------- roster
@check("ticker roster (KOSPI / KOSDAQ)")
def _roster():
    from pykrx import stock
    if not TRADING_DATE:
        return False, "skipped - no trading date"
    ks = stock.get_market_ticker_list(TRADING_DATE, market="KOSPI")
    kq = stock.get_market_ticker_list(TRADING_DATE, market="KOSDAQ")
    if not ks or not kq:
        return False, f"empty roster (KOSPI={len(ks or [])}, KOSDAQ={len(kq or [])})"
    sane = 700 <= len(ks) <= 1100 and 1300 <= len(kq) <= 2200
    note = "" if sane else "  <-- counts look off; expect ~800 / ~1700"
    return True, f"KOSPI={len(ks)}, KOSDAQ={len(kq)}, total={len(ks)+len(kq)}{note}"


# ---------------------------------------------------------------- name
@check("company name lookup")
def _name():
    from pykrx import stock
    n = stock.get_market_ticker_name("005930")
    if not n:
        return False, "no name returned for 005930"
    return True, f"005930 -> {n}   (expect 삼성전자)"


# ---------------------------------------------------- market cap columns
@check("market cap snapshot + COLUMN NAMES")
def _caps():
    from pykrx import stock
    if not TRADING_DATE:
        return False, "skipped"
    df = stock.get_market_cap(TRADING_DATE, market="KOSPI")
    if df is None or df.empty:
        return False, "empty snapshot"
    cols = list(df.columns)
    idx = df.index.name
    expected = {"시가총액", "거래대금"}
    missing = expected - set(cols)
    msg = f"index='{idx}' columns={cols}"
    if missing:
        return False, (f"{msg}\n       MISSING {missing} -> update the rename map "
                       f"in providers_kr.market_cap_snapshot()")
    return True, f"{msg}\n       {len(df)} rows"


# --------------------------------------------------- fundamentals columns
@check("fundamentals snapshot + COLUMN NAMES")
def _fund():
    from pykrx import stock
    if not TRADING_DATE:
        return False, "skipped"
    df = stock.get_market_fundamental(TRADING_DATE, market="KOSPI")
    if df is None or df.empty:
        return False, "empty snapshot"
    cols = list(df.columns)
    missing = {"PER", "PBR", "EPS", "BPS"} - set(cols)
    msg = f"index='{df.index.name}' columns={cols}"
    if missing:
        return False, (f"{msg}\n       MISSING {missing} -> update the rename map "
                       f"in providers_kr.fundamentals_snapshot()")
    per = df["PER"]
    zero = int((per == 0).sum())
    return True, (f"{msg}\n       {len(df)} rows; PER==0 for {zero} names "
                  f"(loss-makers - correctly treated as missing)")


# ---------------------------------------------------------------- yfinance
@check("yfinance enrichment (industry + EV/EBITDA)")
def _yf():
    import yfinance as yf
    info = yf.Ticker("005930.KS").info or {}
    sec, ind = info.get("sector"), info.get("industry")
    if not sec and not ind:
        return False, ("no sector/industry for 005930.KS. yfinance breaks "
                       "periodically;\n       try pip install -U yfinance")
    ev = info.get("enterpriseToEbitda")
    return True, (f"005930.KS -> sector={sec}, industry={ind}, "
                  f"EV/EBITDA={ev}\n       (EV/EBITDA is often None - expected)")


# ---------------------------------------------------------------- FX
@check("KRW FX rate")
def _fx():
    import yfinance as yf
    h = yf.Ticker("KRW=X").history(period="5d")
    if h.empty:
        return False, "no FX data - pass --fx 1386 manually to main_kr.py"
    rate = float(h["Close"].dropna().iloc[-1])
    mcap = 600e6 * rate / 1e8
    adv = 4e6 * rate / 1e8
    return True, (f"1 USD = {rate:,.1f} KRW\n"
                  f"       USD 600m mcap = {mcap:,.0f}억원 | "
                  f"USD 4m ADV = {adv:,.0f}억원")


# ---------------------------------------------------------------- summary
print("=" * 68)
failed = [r for r in results if not r[1]]
if not failed:
    print("ALL CHECKS PASSED -> next: python main_kr.py --board KOSPI "
          "--skip-liquidity -v")
else:
    print(f"{len(failed)} CHECK(S) FAILED:")
    for label, _, msg in failed:
        print(f"  - {label}: {msg.splitlines()[0]}")
    if any(lbl.startswith("KRX credentials") for lbl, _, _ in failed):
        print("\nStart with KRX_ID / KRX_PW. While logged out, data.krx.co.kr returns"
              "\n'LOGOUT' for every cross-sectional call, so the roster,"
              "\nmarket-cap and fundamentals checks cannot pass no matter what"
              "\nthe rename maps in providers_kr.py say.")
    else:
        print("\nFix these before running main_kr.py. Column-name failures are the "
              "most\ncommon and are a one-line edit in providers_kr.py.")
print("=" * 68)
sys.exit(1 if failed else 0)
