"""Hypothesis lab: which 6am/7am range characters and event SEQUENCES predict
fade win rate?

Re-detects every base fade trade (failure + retest models, both slots,
pre-9:30 fills, 1.3:1 to mid) with an extended feature set covering candle
character, prior-hour chains, overnight context, and the sequence of events
between the candle close and the entry. Tests each hypothesis as a
conditional win-rate table with per-slot consistency (6am vs 7am acts as a
pseudo-replication) and Wilson confidence intervals.

Usage: python run_hypothesis_lab.py [path-to-parquet]
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
OUT = Path("results/hypothesis_lab")
OUT.mkdir(parents=True, exist_ok=True)

SLOTS = {"6am": 6, "7am": 7}
RR = 1.3
CUTOFF = 9 * 60 + 30
EOD = 16 * 60


# ---------------------------------------------------------------------------
# detection (extended copy of the sweep logic, returning event-sequence info)
# ---------------------------------------------------------------------------
def detect(o, h, l, c, v, mins, rng_high, rng_low, model, d):
    """Returns dict with seq/break info or None."""
    level = rng_high if d > 0 else rng_low
    other = rng_low if d > 0 else rng_high
    n = len(c)
    if model == "retest":
        brk = np.argmax(c * d > level * d) if ((c * d) > level * d).any() else -1
        if brk < 0:
            return None
        back = -1
        for i in range(brk + 1, n):
            if rng_low <= c[i] <= rng_high:
                back = i
                break
        if back < 0:
            return None
        ext = h[brk:back] if d > 0 else l[brk:back]
        pen = (ext * d).max() - level * d
        seq_end, bars_outside = back, back - brk
    else:
        brkser = h if d > 0 else l
        brk = np.argmax(brkser * d > level * d) if ((brkser * d) > level * d).any() else -1
        if brk < 0:
            return None
        if not (rng_low <= c[brk] <= rng_high):
            return None
        pen = (brkser[brk] - level) * d
        seq_end, bars_outside = brk, 1
    # opposite side swept before this side's break?
    pre_l, pre_h = l[:brk], h[:brk]
    opp_swept = bool((pre_l < other).any() if d > 0 else (pre_h > other).any())
    return dict(brk=brk, seq_end=seq_end, pen=pen, bars_outside=bars_outside,
                opp_swept=opp_swept, level=level)


def hunt_and_resolve(h, l, c, mins, seq_end, d, level, mid, n):
    for k in range(seq_end + 1, n):
        if (l[k] <= mid if d > 0 else h[k] >= mid):
            return None
        if (h[k] >= level if d > 0 else l[k] <= level):
            if mins[k] >= CUTOFF:
                return None
            tp_d = abs(level - mid)
            stop = level + d * tp_d / RR
            for m in range(k, n):
                if (h[m] >= stop if d > 0 else l[m] <= stop):
                    return k, -1.0
                if (l[m] <= mid if d > 0 else h[m] >= mid):
                    return k, RR
            return k, (level - c[n - 1]) * d / (tp_d / RR)
    return None


# ---------------------------------------------------------------------------
def main():
    print("loading data...")
    df = load_continuous(DATA)
    df = df.assign(min=df.index.hour * 60 + df.index.minute)

    # overnight stats per trading date (18:00 prev -> 06:00)
    on = df[(df.index.hour >= 18) | (df.index.hour < 6)]
    on_sess = (on.index + pd.Timedelta(hours=6)).date
    on_stats = on.groupby(on_sess).agg(
        on_open=("open", "first"), on_high=("high", "max"), on_low=("low", "min"))

    rows = []
    for date, day in df.groupby(df.index.date):
        candles = {}
        for s in (4, 5, 6, 7):
            cb = day[(day["min"] >= s * 60) & (day["min"] < (s + 1) * 60)]
            if len(cb) >= 50:
                candles[s] = dict(o=cb["open"].iloc[0], h=cb["high"].max(),
                                  l=cb["low"].min(), c=cb["close"].iloc[-1])
        onr = on_stats.loc[date] if date in on_stats.index else None
        for slot_name, s in SLOTS.items():
            if s not in candles or s - 1 not in candles:
                continue
            cd, pv = candles[s], candles[s - 1]
            rng = cd["h"] - cd["l"]
            pv_rng = pv["h"] - pv["l"]
            if rng <= 0 or pv_rng <= 0:
                continue
            mid = (cd["h"] + cd["l"]) / 2
            post = day[(day["min"] >= (s + 1) * 60) & (day["min"] < EOD)]
            if len(post) < 60:
                continue
            o = post["open"].to_numpy()
            h = post["high"].to_numpy()
            l = post["low"].to_numpy()
            c = post["close"].to_numpy()
            v = post["volume"].to_numpy().astype(float)
            mins = post["min"].to_numpy()
            n = len(c)
            vol_avg = pd.Series(v).rolling(20, min_periods=10).mean().shift(1).to_numpy()

            for model in ("retest", "failure"):
                for d in (+1, -1):
                    info = detect(o, h, l, c, v, mins, cd["h"], cd["l"], model, d)
                    if info is None:
                        continue
                    res = hunt_and_resolve(h, l, c, mins, info["seq_end"], d,
                                           info["level"], mid, n)
                    if res is None:
                        continue
                    k, r = res
                    brk = info["brk"]
                    overlap = (min(cd["h"], pv["h"]) - max(cd["l"], pv["l"])) / rng
                    row = {
                        "date": date, "slot": slot_name, "model": model,
                        "side": "short" if d > 0 else "long", "r": r, "win": r > 0,
                        # candle character
                        "range": rng, "body_ratio": abs(cd["c"] - cd["o"]) / rng,
                        "candle_dir": np.sign(cd["c"] - cd["o"]),
                        "inside_prev": cd["h"] <= pv["h"] and cd["l"] >= pv["l"],
                        "overlap_prev": overlap,
                        "exp_ratio": rng / pv_rng,
                        # event sequence
                        "break_with_candle": (d > 0) == (cd["c"] > cd["o"]),
                        "opp_swept_first": info["opp_swept"],
                        "time_to_break": mins[brk] - (s + 1) * 60,
                        "bars_outside": info["bars_outside"],
                        "entry_delay": mins[k] - mins[info["seq_end"]],
                        "vol_break_ratio": v[brk] / vol_avg[brk] if vol_avg[brk] > 0 else np.nan,
                        "pen_pts": info["pen"],
                        "dow": pd.Timestamp(date).dayofweek,
                    }
                    if onr is not None and onr["on_high"] > onr["on_low"]:
                        on_rng = onr["on_high"] - onr["on_low"]
                        edge = info["level"]
                        row["edge_pos_on"] = ((edge - onr["on_low"]) / on_rng if d > 0
                                              else (onr["on_high"] - edge) / on_rng)
                        row["fade_with_on_trend"] = \
                            (-d) == np.sign(cd["c"] - onr["on_open"])
                    rows.append(row)

    tr = pd.DataFrame(rows)
    # trailing same-slot median for rel_range
    med = (tr.drop_duplicates(["date", "slot"]).sort_values("date")
           .set_index(["date", "slot"])["range"].groupby(level=1)
           .transform(lambda s: s.rolling(20, min_periods=10).median().shift(1)))
    tr["rel_range"] = tr.set_index(["date", "slot"])["range"].div(med).to_numpy()
    tr.to_csv(OUT / "features.csv", index=False)
    base_win = tr["win"].mean()
    print(f"{len(tr)} trades, baseline win rate {base_win:.3f}")

    # ---------------- bucket features -------------------------------------
    tr["overlap_b"] = pd.cut(tr["overlap_prev"], [-np.inf, 0.6, 0.9, np.inf],
                             labels=["low", "mid", "high"])
    tr["ttb_b"] = pd.cut(tr["time_to_break"], [-1, 15, 45, np.inf],
                         labels=["<=15m", "16-45m", ">45m"])
    tr["outside_b"] = pd.cut(tr["bars_outside"], [0, 1, 3, np.inf],
                             labels=["1 bar", "2-3", ">=4"])
    tr["delay_b"] = pd.cut(tr["entry_delay"], [-1, 5, 20, np.inf],
                           labels=["<=5m", "6-20m", ">20m"])
    tr["vol_b"] = pd.qcut(tr["vol_break_ratio"], 3, labels=["low", "mid", "high"])
    tr["edge_on_b"] = pd.cut(tr["edge_pos_on"], [-np.inf, 0.9, 1.0, np.inf],
                             labels=["inside ON", "at ON extreme", "beyond ON"])
    tr["rel_range_b"] = pd.qcut(tr["rel_range"], 3, labels=["small", "mid", "large"])
    tr["pen_b"] = pd.qcut(tr["pen_pts"], 3, labels=["shallow", "mid", "deep"])
    tr["dow_n"] = tr["dow"].map({0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"})

    HYPS = [
        ("H1 inside candle", "inside_prev"),
        ("H2 overlap with prior hour", "overlap_b"),
        ("H3 break against candle dir", "break_with_candle"),
        ("H4 opposite side swept first", "opp_swept_first"),
        ("H5 time to break", "ttb_b"),
        ("H6 bars spent outside", "outside_b"),
        ("H7 broken edge vs overnight range", "edge_on_b"),
        ("H8 fade with overnight trend", "fade_with_on_trend"),
        ("H9 breaking bar volume", "vol_b"),
        ("H10 entry delay after failure", "delay_b"),
        ("H11 day of week", "dow_n"),
        ("ref: penetration", "pen_b"),
        ("ref: relative range", "rel_range_b"),
    ]

    def wilson(p, nn, z=1.96):
        return z * np.sqrt(p * (1 - p) / nn + z**2 / (4 * nn**2)) / (1 + z**2 / nn)

    recs = []
    for hname, col in HYPS:
        for val, g in tr.groupby(col, observed=True):
            if len(g) < 80:
                continue
            w = g["win"].mean()
            w6 = g[g["slot"] == "6am"]["win"].mean()
            w7 = g[g["slot"] == "7am"]["win"].mean()
            recs.append({
                "hypothesis": hname, "bucket": str(val), "n": len(g),
                "win_rate": round(w, 3), "lift": round(w - base_win, 3),
                "ci95": round(wilson(w, len(g)), 3),
                "avg_R": round(g["r"].mean(), 3),
                "win_6am": round(w6, 3), "win_7am": round(w7, 3),
                "consistent": bool(np.sign(w6 - base_win) == np.sign(w7 - base_win)
                                   and abs(w - base_win) > 0.01),
            })
    ht = pd.DataFrame(recs)
    ht.to_csv(OUT / "hypothesis_table.csv", index=False)

    # ---------------- interactions of the strongest singles ----------------
    inter = {}
    inter["H4 x penetration"] = tr.groupby(["opp_swept_first", "pen_b"], observed=True) \
        .agg(n=("win", "size"), win=("win", "mean"), avg_R=("r", "mean")).round(3)
    inter["H3 x rel_range"] = tr.groupby(["break_with_candle", "rel_range_b"], observed=True) \
        .agg(n=("win", "size"), win=("win", "mean"), avg_R=("r", "mean")).round(3)
    inter["H5 x model"] = tr.groupby(["ttb_b", "model"], observed=True) \
        .agg(n=("win", "size"), win=("win", "mean"), avg_R=("r", "mean")).round(3)
    inter["H7 x H4"] = tr.groupby(["edge_on_b", "opp_swept_first"], observed=True) \
        .agg(n=("win", "size"), win=("win", "mean"), avg_R=("r", "mean")).round(3)
    inter["H9 x model"] = tr.groupby(["vol_b", "model"], observed=True) \
        .agg(n=("win", "size"), win=("win", "mean"), avg_R=("r", "mean")).round(3)
    for name, t in inter.items():
        t.to_csv(OUT / f"interaction_{name.replace(' ', '_').replace('x', 'x')}.csv")

    # ---------------- chart -------------------------------------------------
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    ht2 = ht.sort_values("lift")
    fig, ax = plt.subplots(figsize=(9, 0.32 * len(ht2) + 1.5))
    ylab = ht2["hypothesis"] + ": " + ht2["bucket"]
    colors = ["#089981" if c else "#9598a1" for c in ht2["consistent"]]
    ax.barh(ylab, ht2["lift"], xerr=ht2["ci95"], color=colors, alpha=0.85)
    ax.axvline(0, color="k", lw=1)
    ax.set_xlabel(f"win-rate lift vs baseline ({base_win:.1%}); green = consistent across 6am & 7am")
    ax.set_title("Hypothesis lab: conditional win-rate lifts (fade, 1.3:1 to mid)")
    fig.tight_layout()
    fig.savefig(OUT / "lift_forest.png")
    plt.close("all")

    # ---------------- report ------------------------------------------------
    md = lambda t: t.to_markdown()
    lines = [
        "# Hypothesis Lab: what kind of 6am/7am range produces winning fades?",
        "",
        f"{len(tr)} base fade trades (failure + retest, both slots, pre-9:30 "
        f"fills, 1.3:1 to mid). Baseline win rate {base_win:.1%} "
        f"(breakeven at 1.3:1 = 43.5%). 'consistent' requires the lift to have "
        "the same sign in the 6am and 7am populations independently.",
        "",
        "## All hypothesis buckets",
        "", ht.to_markdown(index=False), "",
        "![forest](lift_forest.png)",
        "",
    ]
    for name, t in inter.items():
        lines += [f"## Interaction: {name}", "", md(t), ""]
    (OUT / "REPORT.md").write_text("\n".join(lines))
    print("done -> results/hypothesis_lab/")


if __name__ == "__main__":
    main()
