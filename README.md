# ⚡ F&O Live Screener (Upstox)

Scans **all 210 NSE stock-F&O underlyings** live, filters them through hard gates, then
publishes only the **top 5 longs + top 5 shorts** — each with an entry, stop loss, two
targets, R:R, risk per lot, and the exact reasons it qualified.

Every card also carries a **READY / WAIT / LATE / SKIP** verdict and the eight checks behind
it, so you know whether to act *right now* rather than guessing. There's an in-app
**📗 How to trade it** tab and a matching [`PLAYBOOK.md`](PLAYBOOK.md).

Intraday focus. Longs **and** shorts. Built for your Upstox real-time market data plan.

> 📱 **Want it on your phone?** → [`DEPLOY.md`](DEPLOY.md) — push to GitHub, deploy on
> Render's free tier, add it to your home screen. Includes a password gate, PWA app icon,
> and the daily token routine.

---

## 1. Run it

Nothing to install — plain Python 3.9+ and the standard library.

**On your computer**

```bash
cd fno-screener
python3 server.py            # or: ./run.sh
```

Open **http://localhost:8000**.

**In the cloud (phone-friendly, always reachable)** — repo is already set up for Render:
`render.yaml`, `Procfile`, `requirements.txt` and a `DEPLOY.md` walkthrough. Short version:
push to GitHub → Render **New → Blueprint** → pick the repo → set `APP_PASSWORD` → done.
A `Dockerfile`-free, dependency-free deploy.

First boot downloads Upstox's instrument master (~2.5 MB) and builds the F&O universe from
it, then warms up daily data (previous close, yesterday's high/low, ATR, 20-day averages)
for all 210 stocks. During market hours it polls every 20s and deep-scans 1-minute candles
on the top 40 candidates about once a minute.

## 2. Go live with your Upstox token

**Fastest — no redirect URL needed:** open https://account.upstox.com/developer/apps, click
your app, click **Generate**, copy the token, paste it into **⚙ Settings → Connect Upstox →
“Go live”**. Takes about 30 seconds.

**One-click login (optional):** register `http://localhost:8000/callback` as your app's
Redirect URI, then in Settings put your **API key + API secret** → the app shows you the exact
redirect URL and unlocks **Login with Upstox →** → approves, returns to `/callback`, token
saved automatically. (Or paste the API key + secret here and I'll do it.)

**Any redirect URL works, too:** after approving, the browser lands on
`<your-redirect>?code=XYZ` — even if that page 404s, paste that whole URL (or just the code)
into Settings → “paste the code” → **Finish login**. The secret exchange happens server-side in
this app.

Full walkthrough with the exact URLs, error codes and gotchas: **[UPSTOX_LOGIN.md](UPSTOX_LOGIN.md)**.

Without a token the app runs in **demo mode** (clearly labelled) so you can see the UI and
logic working even on weekends. Tokens stay on your machine (`runtime/`, mode 600) and are
sent nowhere but `api.upstox.com`.

## 3. What it actually does with all 210 stocks

**Stage 1 — snapshot (every cycle, ~3 API calls)**
One batch quote call for all 210 cash symbols + one for their stock futures
(open interest, and the tradeable contract price): LTP, day OHLC, volume, net change,
OI, top-5 bid/ask depth, circuit limits, last trade time.

**Stage 2 — pre-score (all 210)**
Relative strength vs NIFTY · position in the day's range · RVOL (volume vs its own normal
pace for this time of day) · OI build-up (long/short build-up vs covering/unwinding) ·
break of yesterday's high/low · 20-day trend · sector strength.

**Stage 3 — deep scan (top 40)**
1-minute candles per stock → day VWAP, opening-range (9:15–9:30) high/low, 5-min EMA20 slope,
RSI(5m), volume burst in the last 10 minutes, higher-lows/lower-highs structure, how fresh
the day's extreme is, and a today's-price sparkline for the card.

**Stage 4 — hard gates (throw-outs)**
illiquid (pace-adjusted turnover) · wide bid/ask spread · ATR too small to pay · penny prices ·
too close to a circuit limit (you couldn't get filled) · your manual ban list · not yet warmed up.

**Stage 5 — final score & selection**
Weighted composite: relative strength 22% · momentum & intraday structure 20% · level/breakout
structure 18% · volume confirmation 13% · OI build-up 12% · setup quality (R:R, stop width) 10% ·
market + sector alignment 5%. Penalties for being stretched from VWAP, near circuit, wide
spread, or for a move driven only by short covering.

Then: publish top 5 per side **with max 2 names per sector**, and only above the score floor.
The market regime (Risk-On / Risk-Off / Chop, from index trend, breadth and index-vs-VWAP)
raises the bar for the unfavoured side automatically. If nothing clears the bar you get an
honest empty card instead of a forced trade.

## 4. The trade-decision layer

Scores tell you what's strong. Verdicts tell you what to *do*, which is the harder question.

**READY / WAIT / LATE / SKIP** on every card, from eight live checks — score ≥ 62, volume pace
≥ 1.3×, right side of VWAP, not extended > 2× ATR from VWAP, market regime agrees, held on the
list 2+ refreshes, price still at/before the entry, and a tradeable session window. Tap the
verdict to see all eight.

**Session phase bar** — 09:15–09:30 *Avoid* · 09:30–10:15 *Prime* · 10:15–11:30 *Good* ·
11:30–13:30 *Choppy* · 13:30–14:45 *Prime* · 14:45–15:15 *Manage* · 15:15+ *Exit*.
New entries only belong in the Prime/Good windows.

**Stability line** — *“on list 42 min · best rank #1 · score steady”*. Stability beats
freshness: a pick that has held its place is a trend, one that just appeared is noise.

**Anti-chase guard** — if price is already more than 0.6R past the entry zone the card flips
to **LATE** and tells you to wait for a pullback, not to chase.

Full routine (entry modes, T1/T2 exits, sizing formula, a timed day plan):
[`PLAYBOOK.md`](PLAYBOOK.md) or the **📗 How to trade it** tab in the app.

## 5. Reading a card

| Field | Meaning |
|---|---|
| **Score** | 0–100 composite (green ≥72, amber ≥62). Rank order = conviction. |
| **Entry** | green/blue box. *BREAKOUT* = buy strength above the day high, *BUY NOW* = just above VWAP, *BUY DIP* = wait for a pullback to the zone. |
| **Stop** | tightest sensible structural level (below VWAP / opening range / day low), floored so it isn't noise-width. |
| **T1 / T2** | 1R and ~2R, snapped to real levels (yesterday's high/low, 20-day breakout) when they're genuinely further out. |
| **R:R · risk % · risk/lot** | stop distance as a %, and in ₹ for one lot — size directly from this. |
| **RVOL** | volume vs its own normal pace right now. |
| **OI Δ** | open-interest change since the session's first observation (or the previous close, if the app was running then). |
| **Status** | WATCH → WORKING → RUNNING (in profit ≥0.8R) / FADING / STOPPED, plus how far it has moved *since it was flagged*. |

The **📋 All F&O Stocks** tab is the full 210-row scan — sort by any column, search, and see
exactly which gate each stock failed. **📘 How it works** repeats this logic inside the app.

## 6. Settings (⚙)

Refresh interval · deep-scan depth · minimum turnover · max spread · min ATR · min price ·
picks per side · max per sector · score floor · long/short toggles · F&O ban-list symbols ·
**Rebuild F&O list** (re-pulls the instrument master when new stocks enter F&O).

Rate limits are respected automatically: quotes are cheap and plentiful, historical candles
are not, so scan depth and the deep-scan interval are auto-throttled to stay under Upstox's
historical-data limit (default ~95 requests/min).

## 7. Files

```
server.py          API + UI server (stdlib http.server)      engine.py   scoring + selection brain
sources.py         live + demo data sources, market clock   universe.py F&O universe builder + sectors
upstox_client.py   Upstox v3 client (quotes, candles, retry, rate limits)
auth.py            Upstox OAuth (login URL, code exchange, credentials) + /callback page
UPSTOX_LOGIN.md    how to get your daily access token, three ways
PLAYBOOK.md        how to pick, enter, size and exit — the trading routine
DEPLOY.md          GitHub + Render deploy, phone install, daily token routine
render.yaml        Render blueprint (service, env vars, optional disk)
Procfile           start command for other hosts (Railway, Heroku-style)
.env.example       copy to .env for local runs / paste into Render
static/icon-*.png  PWA icons so it installs on your phone home screen
static/index.html  the whole UI (no CDN, works offline)      tests/selftest.py  end-to-end engine test
cache/             instrument master, universe, daily warm-up cache (per session)
logs/              picks_YYYY-MM-DD.csv — every pick as it was issued, for review
runtime/token.txt  your saved access token (never sent anywhere but Upstox)
```

`python3 tests/selftest.py` runs the engine on synthetic data and asserts the whole
pipeline (210 → gates → deep scan → top 5 each side, level ordering, R:R, sector cap).

## 8. Verified against your Upstox account (live)

Confirmed working end-to-end with a real token, using the **v3** quote/candle APIs:

| Check | Result |
|---|---|
| Batch quotes | 210 cash + 210 futures in **one call** (420 keys) — the whole universe per cycle |
| Index feed | NIFTY 23,063.1 (−1.66%), BANKNIFTY 55,438.5 (−2.00%), India VIX 12.69 (+18.4%) |
| Daily candles (210 calls) | 100% coverage, PDH/PDL/ATR/20-day averages built for every stock |
| 1-minute candles | 375 bars/session → VWAP, opening range, 5-min EMA, RSI, volume bursts |
| Open interest | from the exchange's own `previous_oi` → real day-on-day OI build-up |
| Regime read | Risk-Off (breadth 21 up / 188 down, index below VWAP) → shorts favoured |

v3 field notes worth remembering if you extend the code:

- `ohlc.close` in v3 is **this session's** close, not the previous close — the previous close is
  `prev_close_price` (v2 had it the other way round).
- `timestamp` is an ISO string in v3 (`2026-09-24T20:13:41.955+05:30`), while `last_trade_time`
  is still epoch milliseconds.
- `previous_oi`, `average_price` (exchange session average) and `total_buy/sell_quantity`
  (order-book pressure) are all available — the engine uses all three.
- Instrument keys must be percent-encoded **once**; `,` separates multiple keys.

Only 2 of ~300 warm-up calls were throttled (429) and both recovered on retry — the client
backs off automatically. Rate limits default to 80/min for candle calls.

## 9. Session replay — backtest the "First seen" column

The live screen stamps each stock with the first moment it entered the Long or Short table
under the filter you had set. **`replay.py`** answers the obvious follow-up — *when* would it
have entered under a different filter? — by walking a finished session through the **same
`engine.py`**, one minute at a time, with the engine clock pinned to the replayed minute
(RVOL pace, turnover projection and session phase all follow it).

### From the app — just click Replay

The header has a **▤ Replay** button. If a report already exists it opens it; if not, it
**builds one**: the server fetches that session's 1-minute candles from Upstox, walks them
through the engine, and writes the report, reporting progress on the button itself
(`▤ Replay · fetching 42%` → `replaying` → it opens the page by itself). The default target is
the newest session that has finished, and shift-clicking forces a rebuild.

The build runs on a background thread (`POST /api/replay/build`, progress in
`/api/snapshot → app.replay.job`) because fetching a full session is ~2 API calls per stock at
60/min — a few minutes. Keep the tab (or the live screen) open while it runs.

Two things bite on a host like Render's free tier: the token you paste is valid for **that
day**, so the build has to run on the day of the session you want; and the disk is ephemeral,
so a redeploy or a long idle spin-down takes the report **and** its candle cache with it. A
rebuild from the cache is ~20 seconds, but the first build of a session is the slow one.

### By hand

```bash
python3 replay.py                       # last session, no filter (fetches + caches candles)
python3 replay.py --session 2026-10-16 --step 1 --rvol 1.5 --cap 40
python3 replay.py --collect-only         # just fetch/cache the candles
python3 replay_report.py                 # build the filterable HTML report
```

| Output | What it holds |
|---|---|
| `cache/replay/candles_<session>.json.gz` | daily + 1-minute candles for that session, fetched once |
| `logs/replay/replay_<session>_rv…_cl…_cs….csv` | one row per stock **per table**: `first_seen, last_seen, minutes_on, score, other_score, rvol, …` |
| `logs/replay/frames_<session>.json` | every stock's Long/Short score + RVOL for every minute — the raw material for changing filters after the fact |
| `logs/replay/report_<session>.html` | self-contained page (no assets, no API calls) served at **`/replay`** |

In the report, the RVOL / `Short ≤` / `Long ≤` boxes recompute every first-seen time in the
browser using exactly the predicate in `static/index.html`, and the grid underneath shows the
whole filter space at once (stocks that would have entered, and the median time they did).

Past sessions come from Upstox's *dated* historical route — `/v3/historical-candle/{key}/
minutes/1/{to_date}/{from_date}` (`UpstoxClient.historical_candles`). The plain intraday route
the live screen uses only ever serves the session in progress, so it cannot replay yesterday.

The round trip is verified: the replay re-derives the names the live screen logged as picks
that day, and `replay.py` prints that cross-check at the end of every run. Note that first-seen
times are the minutes a filter was **satisfied**, not when the live page happened to refresh.

`python3 tests/offline_replay.py` checks the whole pipeline with no network and no token: the
dated URL, which route is chosen for a finished session vs the current one, a full
collect → replay → report run, and the refusals the build endpoint must make.

## 10. Charts — tapping a stock's name opens your own layout

Tapping a symbol in either table opens **your** TradingView chart in a new tab with the symbol
swapped in: `<your layout URL>?symbol=NSE:<SYMBOL>`.

Use a **saved layout** URL, not the bare `tradingview.com/chart/` one — a layout is what carries
your indicators, colours and drawings. Open that layout on TradingView, copy the address bar, and
paste it into **⚙ Settings → Chart link** (it ships pre-filled with the layout this was built for).
Whatever the URL already had, the app drops its `symbol=` and adds the one you tapped.

Two things this deliberately is *not*:

- **not a popup inside this page.** An embedded TradingView chart is an anonymous widget — it has no
  login, so your saved layout can never load in it. (This is the same reason broker platforms show a
  reduced indicator set.) The first version of this feature was a popup, and a plain chart was
  exactly what it showed.
- **not an iframe to tradingview.com either.** Their chart pages refuse to be framed, and a
  cross-site frame would not carry your login even if they did.

So the chart opens in its own tab: logged in as you, symbol already switched, your setup intact.

## 11. Honest notes

- Picks are **intraday ideas for the current session** — square off before 15:15–15:30 IST unless you deliberately carry over.
- These are **futures** trades: contract and lot size are on each card, and margins apply. Risk per lot is shown so you can size properly.
- Scores are relative: on a dead, choppy day everything scores lower and you may get 1–2 names, not 10. That is intentional.
- The engine uses cash-market candles for VWAP/levels (the standard reference) and futures quotes for OI and the tradeable price; the card shows both.
- Educational/analysis tool, **not investment advice**. Paper-trade it for a few sessions before risking money.
- The chart opens **your TradingView layout** in a new tab — the only way your own indicators come
  along, since an embedded chart cannot sign into your account. Nothing in this app can show a
  private layout inside the page.
