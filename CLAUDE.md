# CLAUDE.md — Korea (KRX) Valuation Screener

Screens KOSPI + KOSDAQ for stocks ≥20% below industry-peer median on PER, PBR,
EV/EBITDA. Gates: market cap ≥ USD 600M, median daily traded value ≥ USD 4M.

## Run order

```bash
python test_korea.py      # offline logic check — must pass, no network needed
python check_setup.py     # tests every live pykrx/yfinance call individually
python main_kr.py -v      # full run, 10–20 min first time

# no KRX account? --source naver runs off KIND + Naver instead (~1 min/board)
python main_kr.py --board KOSPI --skip-liquidity --source naver -v

# each run rewrites kr_dashboard.html in place - open it, re-run, reload

# or serve it, and the Refresh button re-runs the screen for you
python serve.py                      # 127.0.0.1:8765, opens a browser
python serve.py                      # serves BOTH boards: /kospi and /kosdaq
python serve.py --min-roe 8          # extra args go to every board's run

# KOSDAQ is a separate run with its own outputs
python main_kr.py --board KOSDAQ --skip-liquidity --all \n  --out kq_screen_results.csv --dashboard kq_dashboard.html
```

Always run `check_setup.py` before `main_kr.py`. A full run is slow and a
column-name mismatch produces silent all-NaN merges rather than an error.

## Files

| File | Role |
|---|---|
| `screener.py` | Core engine. `sanitize_metrics`, `compute_peer_benchmarks`, `score` are market-agnostic. |
| `config_kr.py` | Thresholds, metric bounds, share-class detection rules. |
| `providers_kr.py` | pykrx (primary) + yfinance (industry, EV/EBITDA). Needs `KRX_ID`/`KRX_PW`. |
| `providers_naver_kr.py` | Fallback source: Naver mobile JSON API + KIND 업종. No account. |
| `korea_filters.py` | Korea share-class hygiene, ROE, value-up flags. |
| `main_kr.py` | CLI. Orders filters so slow per-ticker calls run last. |
| `dashboard.py` | Renders a run into a self-contained HTML dashboard. |
| `serve.py` | Local server behind the dashboard's Refresh button. |
| `build_site.py` | Assembles `site/` for GitHub Pages (noindex, no Refresh button). |
| `dashboard.cmd` | Double-click launcher: starts serve.py and opens the browser. |
| `check_setup.py` | Pre-flight diagnostic. |
| `test_korea.py` | Offline tests with planted traps. Keep green. |

## Invariants — do not remove without understanding why

1. **Preferred shares (우선주) are excluded.** KRX codes: common ends in `0`,
   preferreds reuse the first 5 digits with `5`/`7`/`9` (005930 vs 005935).
   Preferreds trade 20–50% below their commons permanently — no vote, no
   convergence. Include them and *every* preferred line flags as deep value on
   *every* run. This is the single biggest correctness trap in this codebase.

2. **PER/PBR of 0 must be treated as missing, never as cheap.** KRX reports 0
   for loss-makers and negative equity. `METRIC_BOUNDS` lower bounds enforce
   this. Removing them sorts every loss-maker to the top.

3. **Peer groups are `industry × board`, falling back to `industry`.** KOSDAQ
   (growth/biotech) carries structurally richer multiples than KOSPI. Test
   cohorts: machinery ~11x PER vs biotech ~38x. Mixing boards makes any KOSDAQ
   name look expensive and any KOSPI name look cheap.

4. **Peer counts exclude the stock itself**; benchmark is a *winsorized median*,
   not a mean. One 400x PER would drag a mean enough to make average stocks
   look cheap.

5. **`avg_discount` averages across all metrics with data**, including failed
   ones. A stock 40% cheap on PBR and 30% expensive on PER is not a 40%
   discount.

6. **EV/EBITDA is suppressed for financials** — enterprise value isn't
   meaningful for banks and insurers.

7. **Slow calls run last.** `main_kr.py` gates on size/liquidity using cheap
   cross-sectional pykrx calls *before* per-ticker yfinance enrichment. This is
   the difference between a 15-minute run and a 3-hour one. Don't reorder.

8. **The ROE floor runs after scoring, never before.** `apply_roe_gate` narrows
   `passes`; it does not touch `avg_discount`, which still averages every metric
   with data (invariant 5). Gating earlier would change the peer medians
   themselves - the cohort a stock is measured against must include the
   low-ROE names, because that is what makes the cohort representative.
   Missing ROE fails the gate: a company whose EPS or BPS could not be
   resolved is not a company we can call profitable. Default 5%, `--min-roe 0`
   disables.

9. **Three independent screens.** Relative (peer median), absolute
   (`apply_absolute_screen`) and own-history (`apply_history_screen`) are scored
   separately and unioned into `passes_any`; `screen` lists every one a name
   cleared, joined with " + ". None gates another. The absolute tests are
   PBR < 1, EV/EBITDA < 8, PBR < ROE/CoE, dividend yield >= 2%, and the same ROE
   floor; the history test is >= 30% below the company's own five-year median
   on >= 2 of PER, PBR and EV/EBITDA, plus the ROE floor.

10. **Financials clear the absolute screen without EV/EBITDA.** Invariant 6
    suppresses EV/EBITDA for them, so a strict both-metrics rule excludes
    every bank, insurer, broker and holdco - i.e. exactly the names trading
    furthest below book. `abs_financials_pbr_only` (default on) lets them
    qualify on PBR + ROE; those rows carry `abs_via_carveout` so the weaker
    bar stays visible. `--abs-strict-financials` restores the strict reading
    and yields zero financials.

## Known gaps (ranked by value of fixing)

1. **Industry classification comes from yfinance**, not KRX 업종 or WICS.
   Coarse and sometimes wrong. Swapping in a proper WICS/FICS mapping is the
   highest-value upgrade available.
2. **EV/EBITDA is unreliable** — KRX doesn't publish it. OpenDART (free, API
   key) gives full financial statements to compute it properly.
3. **관리종목 and 거래정지 are both filtered now.** Halt status arrives per row
   as `tradeStopType` on the market-value API and matches on ticker, so
   `apply_halt_filter` is exact (KOSPI 25, KOSDAQ 81). 관리종목 still comes
   from KIND by company NAME only, so a renamed company can still slip that
   one. A suspended line carries a stale last price and would otherwise be
   screened as if live.

4. `pbr_bottom20_industry` is a **current cross-section only**. The KRX
   low-PBR disclosure criterion requires two consecutive periods — needs
   history accumulated from dated runs.
5. No forward estimates; trailing multiples only.

## Likely first failures

- **KRX now requires a login.** (Verified 2026-08-22.) As of pykrx 1.2.x,
  `data.krx.co.kr` gates every *cross-sectional* endpoint behind an account:
  roster, market cap, fundamentals, and the index calendar. Logged out it
  answers HTTP 400 with the body `LOGOUT`; pykrx tries to parse that as JSON,
  fails, and it surfaces as `KeyError: '지수명'` — which looks exactly like a
  column-rename bug and is not one. Fix: register free at data.krx.co.kr, set
  `KRX_ID` / `KRX_PW`. Per-ticker calls (`get_market_ohlcv`,
  `get_market_ticker_name`) run off a different backend and keep working while
  logged out, so the failure looks partial rather than total. `check_setup.py`
  tests credentials first, before anything that depends on them.

  `--source naver` sidesteps this entirely (default when the variables are
  absent). See `providers_naver_kr.py` for what differs.

  Column names below cannot be validated until this passes — no data comes
  back to name.

- **pykrx column names.** Code expects `시가총액`, `거래대금`, `PER`, `PBR`,
  `EPS`, `BPS`. `check_setup.py` prints actual columns. Fix = edit the rename
  dicts in `providers_kr.py`. `_call()` already tries multiple function
  spellings across pykrx versions.
- **pandas 3.x may break pykrx** → `pip install 'pandas<3'`.
- **yfinance returns empty `.info`** — breaks periodically when Yahoo changes
  endpoints. Try `pip install -U yfinance`, check their GitHub issues.

## Why the Refresh button needs serve.py

A refresh means scraping KIND and Naver and redoing the peer maths in pandas.
Nothing in a browser can do that: a page opened over `file://` cannot start a
process, and the published Artifact is sandboxed - its runtime capabilities are
`artifact`, `downloads`, `mcp`, `self`, none of which run Python, and its CSP
blocks external hosts outright. So the button is only wired up when something
local is listening.

`dashboard.py` handles this by probing `api/status` on load: answered, it shows
the button; unanswered, it shows the command line instead. The same HTML is
therefore correct off disk, behind `serve.py`, and as a published Artifact -
the published copy is a snapshot by nature, and says so.

## KOSDAQ behaves differently, and that is the data

Same metrics, same framework, very different result - worth knowing before
reading a KOSDAQ run as if it were a KOSPI one.

| | KOSPI | KOSDAQ |
|---|---|---|
| cleared the USD 600m floor | 237 of 808 | 92 of 1,629 |
| median PBR | 1.15 | **5.56** |
| median PER | 14.3 | 27.1 |
| median EV/EBITDA | 9.2 | 21.4 |
| PBR below 1 | 107 | **5** |
| absolute screen passes | 35 | **0** |

Two consequences follow, and neither is a bug:

1. **The absolute screen finds nothing on KOSDAQ.** PBR < 1 and EV/EBITDA < 8
   are deep-value thresholds calibrated for KOSPI. On a growth board with a
   median PBR of 5.6 they are close to unsatisfiable. This is invariant 3's
   warning showing up as an outcome rather than a caution.

2. **Only one industry gets scored relatively.** Of 92 KOSDAQ names spread
   over 28 industries, just 5 industries have the >= 6 members that
   `min_peers = 5` requires, so only 21 rows receive a PER benchmark at all -
   19 of them 특수 목적용 기계 제조업. Invariant 4 is refusing to benchmark
   against noise, correctly, but it means the KOSDAQ relative screen is in
   practice a semiconductor-equipment screen. Lowering `--min-mcap` widens the
   cohorts; lowering `--min-peers` does not fix it, it just benchmarks against
   noise.

## Naver moved, September 2026

The HTML scrape of `finance.naver.com/sise/sise_market_sum` died between
2026-09-10 and 09-11: Naver replaced that page with a client-rendered app.
The request still returns **HTTP 200 with a full-looking document** - there is
simply no `<table>` in it. Scheduled run #12 failed; #11 was the last good one.
If a scrape ever "succeeds" but yields zero rows, check for this shape of
failure before assuming a network or rate-limit problem.

Replaced by `m.stock.naver.com/api/stocks/marketValue/{board}` (JSON, 100 per
page). It is better than what it replaced on every axis: exact KRW rather than
rounded 억원, real 거래대금 rather than a volume x close proxy, `stockEndType`
separating stocks from ETFs and ETNs without needing KIND for it, and
`tradeStopType` per row, which closed known gap #3.

The one thing it does NOT carry is per-share fundamentals. PER, PBR, EPS, BPS,
ROE and DPS now come per ticker from `m.stock.naver.com/api/stock/{code}/
finance/annual`, after the size gate - invariant 7 still holds, because the
gate itself runs on the cheap cross-sectional call. PER and PBR are struck
against today's close over the latest reported EPS/BPS rather than taken from
Naver's year-end figures.

## Three-year history

`fetch_financials` returns revenue, operating profit and net profit for the
last three filed years plus EBITDA, all in 억원 oldest-first, with a compound
rate per metric.

- **Consensus periods are dropped.** The API flags forecasts with
  `isConsensus: "Y"`; including them would report analyst estimates as history.
- **EBITDA is not in Naver's data at all** - there is no depreciation line - so
  it comes from yfinance's income statement. The check that the two sources
  describe the same company: yfinance's operating income matches Naver's
  영업이익 exactly (삼성전자 2025, 436,011억 in both). Banks have no EBITDA in
  either, consistent with invariant 6.
- **Two growth measures, one control.** The dashboard's "Growth shown as"
  selector switches all three columns between latest year-on-year and the 3y
  compound rate; the sparklines and the tooltip (every year, every YoY step,
  the CAGR) are the same either way. They answer different questions - 세방전지
  runs +13% CAGR on revenue but only +4% in the latest year, which is
  deceleration the compound rate hides.
- **YoY stays defined where CAGR does not.** A period-over-period change needs
  no root, so dividing by |base| is well defined for a negative base and gives
  the right sign for the direction of travel. A loss narrowing from -100 to -50
  therefore reads +50%: an improvement, not a profit. The red bars and the raw
  figures in the tooltip are what stop that being misread.
- **CAGR is undefined when the starting year is zero or negative**, and is
  reported as missing rather than as a number with a meaningless sign. 55 of
  240 KOSPI names hit this. The yearly figures always ship alongside the rate,
  so a turnaround like 한국전력 (-47,161억 -> +86,667억) is still visible.

## Own five-year history

The third screen compares today's PER, PBR and EV/EBITDA with the median of
the company's last five FILED years (2021-2025 as of Sept 2026), from
WiseReport's 투자지표 tab (`cF4002.aspx`, `rpt=5`). The 2026(E) column is an
analyst estimate and is dropped. It catches what the other two screens cannot:
a premium company that has de-rated. On KOSDAQ, where the absolute screen finds
nothing, it found 7 names (JYP, 에스엠, 클래시스...) and took the total from 4
to 9.

- **Same rules as the peer screen.** Median, not mean (invariant 4) - 삼성전자
  ran 36.8x PER in 2023, which would drag a mean. Non-positive and out-of-bounds
  multiples are missing, in history as well as today (invariant 2), so a loss
  year drops out of the benchmark rather than distorting it. EV/EBITDA is
  skipped for financials (invariant 6). Fewer than 3 usable years: no benchmark.
- **PER and PBR are comparable across the two providers; EV/EBITDA was not.**
  WiseReport's EPS and BPS match Naver's exactly (8 of 8, including a bank and a
  loss-maker - both are FnGuide underneath). yfinance's current EV/EBITDA did
  NOT match WiseReport's history: only 18 of 30 within +/-25%, holdcos off by up
  to 5x. At a 30% threshold that gap would manufacture signals, so current
  EV/EBITDA is rebuilt from WiseReport's own figures (`current_ev_ebitda`):
  carry forward its latest non-equity EV and move only equity by today's price.
  Do not "simplify" this back to yfinance.
- **Trailing multiples cut both ways here.** When earnings surge, the latest
  filing lags the price and a stock reads EXPENSIVE against its history until
  the next filing catches up (삼성전자, Sept 2026). A one-off gain does the
  opposite: 대웅제약 reads 85% below its PER history. It still passes
  legitimately because PBR - which a one-off does not move - is 36% below too.
- **2 of 3, like the peer screen.** A name can clear while expensive on the
  third metric - 한국카본 passes while 50% above its PBR history. The
  `hist_avg_disc` column averages all three and shows the truth. Requiring all 3
  would also exclude every financial, which has no EV/EBITDA.

One WiseReport session token (`encparam`) serves every ticker; 40 names came
back in 8s with 6 workers and no throttling.

## Publishing

Live: https://shkon1215-netizen.github.io/korea-screener/ (KOSDAQ at
`/kosdaq.html`). Unlisted - `noindex` plus a blanket `robots.txt` - but the
repo itself is public, which free Pages requires. No screen output is
committed; results are regenerated on every run.


`.github/workflows/screen.yml` runs both boards at 07:30 UTC (16:30 KST) on
weekdays, builds `site/`, and deploys to GitHub Pages. KOSDAQ is
`continue-on-error`: a third-party scrape failing should not take the KOSPI
page down with it.

The published pages have no Refresh button - there is no Python behind static
hosting - so `build_site.py` replaces it with the rebuild schedule rather than
showing a control that cannot work. Pages are `noindex, nofollow` plus a
blanket `robots.txt`: unlisted, not secret.

**Naver and KIND do answer GitHub's runners** - confirmed on run #1,
2026-08-30, both boards scraped clean from an Azure/US IP. Worth re-checking if
the scrape steps ever start failing in CI while working locally, since that
would point at IP-based blocking rather than a code change.

## Adjustable thresholds

The dashboard re-evaluates BOTH screens in the browser. Every input they need
travels with each row - the three per-metric discounts, PBR, EV/EBITDA, PER,
ROE, dividend yield, and a `fin` flag for the carve-out - so changing a number
re-runs the verdict without re-running Python. `evaluate()` in the template
mirrors `apply_roe_gate` and `apply_absolute_screen`, including the rule that a
missing value fails a test it is subject to. Settings persist per board in
localStorage; Reset returns to the published run.

Two things are deliberately NOT adjustable, because they cannot be recomputed
from the shipped rows:

- **Peer medians.** Fixed when the run built its cohorts. The per-metric
  discounts can be re-thresholded, but the benchmark behind them cannot move.
- **Market cap below the run's floor.** Those rows were gated out before
  scoring (invariant 7) and are simply absent. The control filters upward
  freely and says so; going lower needs a re-run with `--min-mcap`.

If the client-side verdict at default settings ever disagrees with the Python
funnel, that is a real bug - they are computing the same thing twice.

## Interpretation

Sort by `avg_discount`, then read `roe_pct` immediately. Low PBR + high ROE is
a possible mispricing; low PBR + low ROE is arithmetic — a company not earning
its cost of capital, correctly priced. Much of the "Korea Discount" is the
second kind. The bundled test has a holdco passing at 51% discount with 2.0%
ROE as a worked example.

Low-PBR Korea is a crowded trade (Value-Up index at records, ETFs >₩4tn).
Assume obvious names are found.

Research tool, not investment advice.
