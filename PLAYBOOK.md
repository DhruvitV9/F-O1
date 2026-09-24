# 📗 How to actually pick a stock from the list

The screener does the filtering. **This file is the discipline.** Read it once,
then follow the chips on the cards — the app checks all of this for you live.

> The list will shuffle during the session. That's momentum being momentum, not a
> problem to fix. What matters is that *you* only act in specific windows, on
> specific signals. Everything below is built around that.

---

## The one-line answer

**Trade the #1 or #2 READY card, in the right half of the day, at its stated entry,
with the stop on from the first second. One trade at a time. Two a day, then stop.**

---

## 1 · Only act in the right part of the day

The phase bar at the top of the app tells you where you are. New entries belong
only in these windows:

| IST time | Phase | What you do |
|---|---|---|
| 09:15–09:30 | **Avoid** | Opening range is forming. Look, don't touch. |
| 09:30–10:15 | **Prime** | Best window for BREAKOUT / BREAKDOWN entries. Take your main trade here. |
| 10:15–11:30 | **Good** | Trend continuation — BUY NOW / pullback entries. |
| 11:30–13:30 | **Choppy** | Lunch. Only A+ setups, expect false breakouts. Usually skip. |
| 13:30–14:45 | **Prime** | The afternoon move often decides the day. Second (final) look here. |
| 14:45–15:15 | **Manage** | No new trades. Trail stops, bank profits. |
| 15:15–15:30 | **Exit** | Square off. F&O intraday positions must be closed. |

**Why this matters more than stock selection:** most intraday losses come from
taking a decent setup at 11:45 or 15:05. The same setup at 09:40 works.

---

## 2 · The READY chip — your actual green light

Every card carries a verdict. **Only `READY` means act.** Tap it to see the eight
checks behind it:

| Check | What it means |
|---|---|
| **Score** | Composite ≥ 62–65. Below that it's just a moving stock, not an edge. |
| **Volume pace** | RVOL ≥ 1.3× — real participation, not drift. |
| **Right side of VWAP** | Longs above day VWAP, shorts below. No exceptions. |
| **Not extended** | Within 2× ATR of VWAP — not a vertical spike you'd buy the top of. |
| **Regime** | Market bias agrees (Risk-Off day → shorts get the tick). |
| **Held on the list** | Present for 2+ refreshes. Fresh names are guilty until proven stable. |
| **Entry zone** | Price still at or before the entry. Once it's >0.6R past, it's `LATE`. |
| **Session window** | Phase is tradeable (see the table above). |

| Verdict | Meaning |
|---|---|
| **READY** | All eight pass — go to the entry zone and execute. |
| **WAIT** | Nearly there; one or two things missing (usually volume or stability). Watch it. |
| **LATE** | It already ran. Don't chase — put it on the list for a pullback to the entry zone. |
| **SKIP** | Fails a hard rule (extended, wrong side of VWAP, wrong window). Leave it. |
| **NEXT SESSION** | Market closed. These are tomorrow's levels — re-check after 09:30. |

---

## 3 · Choosing between two good-looking picks

In order:

1. **Higher score** wins — but only among READY cards.
2. **Stability beats freshness.** "On list 42 min · best rank #1 · score steady" is a
   real trend. A card that just appeared is noise until it survives 2 refreshes.
3. **Side with the regime.** On a Risk-Off day, the best short > the best long.
4. **Sector tailwind.** The header shows strongest/weakest sectors by our own 210-name
   breadth. A stock riding a hot sector has help; one fighting its sector doesn't.
5. **Real build-up, not covering.** "Fresh long/short build-up (OI up with price)" keeps
   going. "Short covering" rallies fade — the card warns you.
6. **One per sector.** Two banks is one trade with double the risk.

---

## 4 · Entering (the anti-chase rules)

- Read the **mode line**, it's part of the plan:
  - `BREAKOUT` → buy strength above the day high (only in Prime windows)
  - `BUY NOW` → price is just above VWAP support, take it here
  - `BUY DIP` → wait for the pullback into the entry zone
  - `BREAKDOWN` / `SELL NOW` / `SELL RALLY` → the short-side equivalents
- **If price doesn't come to your entry zone, there is no trade.** Missing a move
  costs nothing; chasing one costs money.
- **Stop goes in with the entry**, not after. Risk per lot is printed on the card.
- If it's `LATE`, you missed *this* move. Wait for a pullback or the next pick.

## 5 · Exiting

- **T1** → book half, move stop to breakeven (card flips WORKING → RUNNING).
- **T2** → book the rest, or trail under each new 5-min higher low (long) / lower high (short).
- **Stop hit → done.** No averaging down, ever. The card marks it STOPPED.
- **Time stop:** no T1 within ~45–60 min and volume fading → take what's there.
- **SIGNAL FADE:** if the card turns `FADING` or the score drops hard for 2–3 refreshes,
  tighten or exit. The reason your trade existed has gone.

## 6 · Sizing (the part that decides whether you survive)

```
lots = (money you're willing to lose on this trade) ÷ (risk per lot shown on the card)
```

Round **down**. If ₹5,000 risk and the card says ₹4,113/lot → 1 lot. Two trades a day.
Two losses → stop for the day, the tape isn't matching the system today.

## 7 · Handling the churn (your specific worry)

- **Don't re-pick every refresh.** Decide once in 09:30–10:15, once more after 13:30.
  In between you *manage*, you don't shop.
- **A pick disappearing is information**, not a signal to chase whatever replaced it.
- **Ignore anything marked fresh** unless it's READY for 2+ consecutive refreshes.
- **Tune the sensitivity** if it feels too twitchy: Settings → *Minimum score*, *Deep-scan
  depth*, *Max per sector*, *Min turnover*. Fewer, better candidates = less churn.
- **At the day's end**, open `logs/live/picks_YYYY-MM-DD.csv`. Every pick is timestamped
  with its levels — compare against what price did and you'll quickly learn which of these
  patterns works in your hands.

## 8 · A real day, start to finish

| Time | Action |
|---|---|
| 09:10 | Token pasted, warm-up finished, read regime + sector leaders. |
| 09:30 | Top Picks tab → tick **actionable only** → read the READY cards' checks. |
| 09:35 | Take **one**. Enter only in the zone, stop on immediately. |
| 10:30 | At T1? Book half, trail the rest. Fading with no volume? Exit flat. |
| 13:35 | Second look — only if you're flat and something is READY. |
| 14:45 | Stop trading. Manage or exit. |
| 15:10 | Everything closed. Skim the CSV. |

---

*Educational tool, not investment advice. Paper-trade this routine for a few sessions —
pick the trade, write down the entry/stop/target, then check what happened — before
risking real money.*
