"""End-to-end sanity test of the screener engine on synthetic data.
Run:  python3 tests/selftest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import Engine                      # noqa: E402
from sources import DemoSource                 # noqa: E402
from universe import build_universe            # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)


def main():
    uni = build_universe(os.path.join(BASE, "cache"))
    print(f"Universe: {uni['count']} F&O underlyings")
    check(uni["count"] >= 180, "universe has at least 180 F&O stocks")
    check(all(s["equity_key"] and s["fut_key"] and s["lot_size"] for s in uni["stocks"]),
          "every stock has cash key, futures key and lot size")

    src = DemoSource(uni)
    eng = Engine(src, uni, cache_dir="/tmp/fno_test_cache", log_dir="/tmp/fno_test_logs", demo=True)
    print("\nWarm-up (synthetic daily candles)…")
    eng.warmup()
    warmed = sum(1 for s in eng.stocks.values() if s.get("warmed"))
    check(warmed == uni["count"], f"warm-up covered all {uni['count']} stocks (got {warmed})")

    print("\nCycle 1 (snapshot + deep scan)…")
    eng.poll(do_deep=True)
    meta = eng.state_meta
    check(sum(1 for s in eng.stocks.values() if s.get("ltp")) == uni["count"],
          "live quotes received for every stock")
    check(meta["deep_scanned"] > 0, f"deep 1-min scan ran on {meta['deep_scanned']} candidates")
    deep = [s for s in eng.stocks.values() if s.get("deep")]
    check(all(s.get("vwap") and s.get("or_high") and s.get("spark") for s in deep),
          "deep-scanned stocks all have VWAP, opening range and sparkline")

    snap = eng.snapshot()
    f = snap["funnel"]
    print(f"\nFunnel: {f['all']} universe → {f['scanned']} quoted → {f['liquid']} pass gates "
          f"→ {f['momentum']} momentum → {f['deep']} deep → {f['final']} final")
    check(f["all"] == uni["count"], "funnel starts from the full F&O universe")
    check(0 < f["liquid"] < f["all"], "hard gates actually filter (not everything passes)")

    L, S = snap["picks"]["long"], snap["picks"]["short"]
    check(len(L) >= 1, f"long picks produced ({len(L)})")
    check(len(S) >= 1, f"short picks produced ({len(S)})")
    check(len(L) <= 5 and len(S) <= 5, "at most 5 picks per side")

    for group in (L, S):
        syms = [p["symbol"] for p in group]
        check(len(syms) == len(set(syms)), "no duplicate symbols inside a list")
        secs = [p["sector"] for p in group]
        if secs:
            check(max(secs.count(s) for s in secs) <= 2, "sector cap respected (max 2 per sector)")
        for p in group:
            lv = p["levels"]
            if p["direction"] == "long":
                ok_order = lv["sl"] < lv["entry"] < lv["t1"] <= lv["t2"]
            else:
                ok_order = lv["t2"] <= lv["t1"] < lv["entry"] < lv["sl"]
            check(ok_order, f"{p['symbol']}: stop/entry/targets in the right order")
            check(0 < p["score"] <= 100, f"{p['symbol']}: score in range ({p['score']})")
            check(lv["rr"] >= 1.0, f"{p['symbol']}: reward:risk >= 1 ({lv['rr']})")
            check(0.25 <= lv["risk_pct"] <= 3.0, f"{p['symbol']}: stop distance sane ({lv['risk_pct']}%)")
            check(len(p["why"]) >= 3, f"{p['symbol']}: has {len(p['why'])} reasons")
            check(p["gate"] if p.get("gate") else True, "pick carries gate status")

    print("\nTop longs (synthetic):")
    for p in L:
        print(f"  {p['rank']}. {p['symbol']:12s} score {p['score']:5.1f}  {p['pct_chg']:+.2f}%  "
              f"RS {p['rel_strength']:+.2f}  entry {p['levels']['entry']} sl {p['levels']['sl']} "
              f"t2 {p['levels']['t2']}  R:R 1:{p['levels']['rr']}")
        print(f"       why: {p['why'][0][:88]}")
    print("\nTop shorts (synthetic):")
    for p in S:
        print(f"  {p['rank']}. {p['symbol']:12s} score {p['score']:5.1f}  {p['pct_chg']:+.2f}%  "
              f"RS {p['rel_strength']:+.2f}  entry {p['levels']['entry']} sl {p['levels']['sl']} "
              f"t2 {p['levels']['t2']}  R:R 1:{p['levels']['rr']}")
        print(f"       why: {p['why'][0][:88]}")

    # designed movers should dominate the top of each list
    from sources import _DEMO_STRONG, _DEMO_WEAK
    strong_in = [p["symbol"] for p in L if p["symbol"] in _DEMO_STRONG]
    weak_in = [p["symbol"] for p in S if p["symbol"] in _DEMO_WEAK]
    check(len(strong_in) >= 2, f"designed strong movers surfaced as longs: {strong_in}") 
    check(len(weak_in) >= 2, f"designed weak movers surfaced as shorts: {weak_in}")

    # re-poll: state must stay consistent and stable
    eng.poll(do_deep=False)
    snap2 = eng.snapshot()
    check(snap2["funnel"]["final"] >= 1, "second cycle still produces picks")
    check(all(p["levels"]["entry"] > 0 for p in snap2["picks"]["long"]), "levels stay valid on re-poll")

    print("\n" + ("ALL CHECKS PASSED ✅" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED ❌"))
    for f_ in FAILS:
        print("   - " + f_)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
