# Korea (KRX) Relative Valuation Screener

Finds KOSPI and KOSDAQ stocks trading at a discount to their industry peers on
PER, PBR, and EV/EBITDA.

**Defaults:** market cap ≥ USD 600M (~₩832bn) · median daily traded value ≥
USD 4M (~₩5.5bn) · ≥20% below peer median on ≥2 of 3 metrics.

## Setup

```bash
pip install -r requirements.txt
python test_korea.py     # offline logic check, no network needed
python main_kr.py -v     # full KOSPI + KOSDAQ run
```

Expect the first live run to take several minutes: ~60 cross-sectional calls
for liquidity history, then one yfinance lookup per surviving name. Results
cache to `.kr_cache/` for 20 hours, so re-runs are fast.

```bash
python main_kr.py --board KOSPI              # KOSPI only
python main_kr.py --discount 0.15 --min-metrics 1   # looser
python main_kr.py --exclude-holdcos --all    # drop holdcos, keep all rows
python main_kr.py --fx 1386                  # pin the FX rate
```

## What changed from the global version

**The data problem disappears.** `pykrx` reads KRX's own published numbers —
complete KOSPI and KOSDAQ rosters, market cap, 거래대금, and PER/PBR/EPS/BPS/DIV.
The global build's binding constraint was that yfinance has no endpoint
enumerating listings, so you had to source a universe yourself. Here the full
~2,600-name universe is one call, free, and official.

**One currency, one FX rate.** The global build had to resolve and validate
~30 currencies and handle pence-denominated quirks. Here it's a scalar.

**The region dimension frees up for the listing board.** Globally, peer groups
had to be industry × region because Japan-vs-Europe multiple gaps swamp
company-level signal. Korea-only holds the country effect constant, so
industry-alone peers become legitimate — and the freed dimension goes to
**KOSPI vs KOSDAQ**. The test cohorts show why: machinery peers at 11x PER
against biotech peers at 38x. A KOSDAQ biotech at 30x is expensive against
machinery and cheap against its own board. Peer groups are `industry × board`
with fallback to `industry` when a cell is thin.

**`sanitize_metrics`, `compute_peer_benchmarks`, and `score` are unchanged**
from the global build. The relative-valuation maths doesn't care which market
it points at. Only the gates differ.

## Korea-specific traps this handles

**Preferred shares (우선주) — the big one.** KRX codes put common stock at a
final digit of `0`; preferred lines reuse the first five digits with `5`
(구형우선주) or `7`/`9` (신형우선주). Samsung Electronics is `005930` common,
`005935` preferred. Preferreds trade at persistent 20–50% discounts to their
commons because they carry no vote — permanently, structurally, with no
convergence. A screener that ignores this flags *every preferred line on the
exchange* as deep value on every run. Excluded by default; `--include-preferred`
to see the damage.

**Holding companies (지주/홀딩스).** Persistent 50–70% NAV discounts that have
not closed in decades. Flagged rather than dropped by default, since some are
legitimately mispriced — `--exclude-holdcos` to remove them. The bundled test
shows one passing the valuation screen at a 51% discount with 2.0% ROE, which
is what cheap-for-cause looks like.

**KRX reports PER and PBR as 0 for loss-makers and negative-equity firms.**
The metric bounds turn those into missing values, never into "cheap." Without
this, every loss-making company sorts to the top.

**SPACs (스팩) and REITs (리츠)** are excluded — shells with no operations and
NAV-based vehicles respectively, neither comparable on earnings multiples.

## Value-up context

KRX has been building a regime that publicly identifies listed companies whose
PBR sits in the bottom 20% of their industry across consecutive reporting
periods, with firms that file improvement plans able to avoid disclosure.
Separately, ruling-party legislation has been proposed to mandate value
enhancement plans for firms below 1.0 PBR. The output therefore carries
`pbr_bottom20_industry` and `pbr_below_1` flags — an industry-relative PBR
screen is close to the regulatory criterion itself.

Two caveats. The flag computes the **current cross-section only**; the actual
criterion requires the condition to hold across two consecutive periods, which
needs history you'd accumulate by saving dated runs. And low-PBR Korea is a
**well-populated trade** — Value-up ETFs have drawn multiple trillions of won
and the index has been setting records, so assume the obvious names are found.

## Output columns

Per metric: raw value, `_peer_median`, `_discount` (positive = cheaper),
`_peer_n`, `_pct_rank`, `_peer_basis`. Plus `roe_pct` (derived from KRX's own
EPS/BPS, so it's consistent with the PER and PBR being screened), `div_yield`,
the value-up flags, and `is_holdco`.

**Read `roe_pct` alongside every hit.** Low PBR with high ROE is a possible
mispricing; low PBR with low ROE is usually arithmetic — the market is
correctly pricing a company that doesn't earn its cost of capital. Korea has a
lot of the second kind, which is much of what the Korea Discount consists of.

CSV is written as `utf-8-sig` so Hangul opens correctly in Excel.

## Known limitations

- **Industry classification comes from yfinance**, not KRX's own 업종 or WICS.
  It's coarse and occasionally wrong. If you have a WICS/FICS mapping, feeding
  it in place of the enrichment step is the single biggest quality upgrade.
  KRX sector indices via `get_index_portfolio_deposit_file` are an official
  free alternative, but cover KOSPI only.
- **EV/EBITDA is the weak metric.** KRX doesn't publish it, so it comes from
  yfinance and goes missing often. PER and PBR are the reliable pair; consider
  `--min-metrics 2` meaning both of those. OpenDART (free, API key required)
  gives full financial statements if you want to compute EV/EBITDA properly.
- **EV/EBITDA is suppressed for financials**, where enterprise value isn't
  meaningful — this removes banks and insurers from that metric entirely.
- **Trailing multiples only.** No forward estimates.
- **관리종목 / 거래정지** (administrative issue, trading halt) aren't filtered.
  Worth adding before trusting any single name.
- The `pykrx` function names have shifted across versions; `providers_kr.py`
  tries multiple spellings, but a major version bump may still break it.

## Verify before trusting

I could not test the live `pykrx` and `yfinance` calls — only the screening
logic, which is fully covered by `test_korea.py`. On your first run, check
that the roster count is plausible (~800 KOSPI, ~1,700 KOSDAQ), that PER/PBR
populated for most names, and hand-check three or four hits against Naver
Finance or KIND before acting on anything.

This is a research tool, not investment advice.
