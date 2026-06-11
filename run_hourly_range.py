"""Hourly-candle breakout/retest and breakout-failure models (Mc5calpAfee style).

Ranges: the 6-7am ET and 7-8am ET hourly candles on NQ. Setups play out on 1m
bars starting the hour after the range completes (7:00 / 8:00 ET).

Both models fade a broken level back to the 50% retrace (range midpoint):

* Breakout/Retest: a 1m close OUTSIDE the range -> a 1m close back INSIDE the
  range -> limit entry on the retest of the broken level.
* Breakout Failure: the FIRST 1m bar to break the level closes back inside
  (wick-out) -> limit entry on the retest of the broken level. If the first
  breaking bar closes outside, no failure setup for that side that day.

Shared rules:
* Target = range midpoint. Stop sized for 1.3:1 reward:risk
  (stop distance = target distance / 1.3). Win = +1.3R, loss = -1R.
* No trade if price tags the midpoint before the retest fills. A bar that
  touches both the midpoint and the level is counted as a cancel (conservative).
* On the entry bar and after, same-bar stop+target counts as stop.
* Base case: entries must fill before 9:30 ET (pre-market only); open trades
  managed to 16:00, EOD exit at the close. A fills-allowed-all-day variant is
  reported separately.
* Variant: move stop to breakeven once the 25% retrace level trades.

Usage: python run_hourly_range.py [path-to-parquet]
"""

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
OUT = Path("results/hourly_range_1m")
OUT.mkdir(parents=True, exist_ok=True)

RANGES = {"6am": (6, 7), "7am": (7, 8)}
RR = 1.3
ENTRY_CUTOFF_MIN = 9 * 60 + 30   # fills must occur before 9:30 ET (base case)
EOD_MIN = 16 * 60


@dataclass
class Trade:
    date: object
    range_name: str
    model: str                # 'retest' or 'failure'
    side: str                 # 'short' fades a broken high, 'long' a broken low
    range_width: float
    entry_minute: int         # minutes after midnight ET
    entry: float
    stop: float
    target: float
    stop_pts: float
    late_fill: bool           # filled at/after 9:30 (variant population)
    outcome: str = ""         # 'win', 'loss', 'eod'
    r: float = np.nan
    r_be25: float = np.nan    # with stop moved to breakeven once 25% level trades
    minutes_to_exit: float = np.nan


def _resolve(h, l, c, mins, k, d, entry, stop, target, be_level):
    """Walk bars from the entry bar. d=+1 broken high (short), so favorable is
    DOWN. Returns (outcome, r, r_be25, minutes_to_exit)."""
    n = len(c)
    stop_d = abs(stop - entry)
    be_armed = False
    be_stop = stop
    out = outb = None
    for m in range(k, n):
        adverse = h[m] >= stop if d > 0 else l[m] <= stop
        favorable = l[m] <= target if d > 0 else h[m] >= target
        if out is None and adverse:
            out = ("loss", -1.0, mins[m])
        if outb is None:
            adv_b = h[m] >= be_stop if d > 0 else l[m] <= be_stop
            if adv_b:
                outb = ("loss", -1.0 if not be_armed else 0.0, mins[m])
        if favorable:
            if out is None:
                out = ("win", RR, mins[m])
            if outb is None:
                outb = ("win", RR, mins[m])
        if not be_armed and ((l[m] <= be_level) if d > 0 else (h[m] >= be_level)):
            be_armed = True
            be_stop = entry
        if out is not None and outb is not None:
            break
    eod_r = (entry - c[n - 1]) * d / stop_d
    if out is None:
        out = ("eod", eod_r, mins[n - 1])
    if outb is None:
        outb = ("eod", eod_r, mins[n - 1])
    return out[0], out[1], outb[1], out[2]


def _scan(day, rng_high, rng_low, start_min, model, d, date, range_name):
    """d=+1: broken high faded short; d=-1: broken low faded long."""
    bars = day[(day["min"] >= start_min) & (day["min"] < EOD_MIN)]
    if len(bars) < 30:
        return None
    o = bars["open"].to_numpy()
    h = bars["high"].to_numpy()
    l = bars["low"].to_numpy()
    c = bars["close"].to_numpy()
    mins = bars["min"].to_numpy()
    n = len(c)
    level = rng_high if d > 0 else rng_low
    mid = (rng_high + rng_low) / 2

    # --- find the bar after which we hunt the retest ------------------------
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
        seq_end = back
    else:  # failure: first bar to BREAK the level must close back inside
        brkser = h if d > 0 else l
        brk = np.argmax(brkser * d > level * d) if ((brkser * d) > level * d).any() else -1
        if brk < 0:
            return None
        if not (rng_low <= c[brk] <= rng_high):
            return None
        seq_end = brk

    # --- retest hunt with the 50%-first cancel ------------------------------
    for k in range(seq_end + 1, n):
        tagged_mid = l[k] <= mid if d > 0 else h[k] >= mid
        retest = h[k] >= level if d > 0 else l[k] <= level
        if tagged_mid:
            return None        # mid first (or ambiguous same-bar): no trade
        if retest:
            entry = level
            tp_d = abs(entry - mid)
            stop = entry + d * tp_d / RR
            be_level = entry - d * tp_d / 2   # the 25% retrace of the range
            late = mins[k] >= ENTRY_CUTOFF_MIN
            outcome, r, r_be, exit_min = _resolve(h, l, c, mins, k, d, entry, stop, mid, be_level)
            return Trade(
                date=date, range_name=range_name, model=model,
                side="short" if d > 0 else "long",
                range_width=rng_high - rng_low, entry_minute=int(mins[k]),
                entry=entry, stop=stop, target=mid, stop_pts=tp_d / RR,
                late_fill=late, outcome=outcome, r=r, r_be25=r_be,
                minutes_to_exit=exit_min - mins[k])
    return None


def main():
    print("loading data...")
    df = load_continuous(DATA)
    df = df.assign(min=df.index.hour * 60 + df.index.minute)

    trades = []
    for date, day in df.groupby(df.index.date):
        for range_name, (h0, h1) in RANGES.items():
            rng = day[(day["min"] >= h0 * 60) & (day["min"] < h1 * 60)]
            if len(rng) < 50:
                continue
            rng_high, rng_low = rng["high"].max(), rng["low"].min()
            for model in ("retest", "failure"):
                for d in (+1, -1):
                    t = _scan(day, rng_high, rng_low, h1 * 60, model, d, date, range_name)
                    if t is not None:
                        trades.append(t)

    tr = pd.DataFrame([asdict(t) for t in trades])
    tr.to_csv(OUT / "trades.csv", index=False)
    base = tr[~tr["late_fill"]]
    print(f"{len(tr)} trades total, {len(base)} with pre-9:30 fills (base case)")

    def summarize(d, rcol="r"):
        r = d[rcol]
        return pd.Series({
            "n": len(d), "win_rate": (d["outcome"] == "win").mean(),
            "loss_rate": (d["outcome"] == "loss").mean(),
            "eod_rate": (d["outcome"] == "eod").mean(),
            "avg_R": r.mean(), "total_R": r.sum(),
            "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
            "median_stop_pts": d["stop_pts"].median(),
            "median_mins_to_exit": d["minutes_to_exit"].median(),
        })

    g = base.groupby(["range_name", "model"]).apply(summarize).round(3)
    g.to_csv(OUT / "summary_base.csv")

    gbe = base.groupby(["range_name", "model"]).apply(
        lambda d: summarize(d, "r_be25")).round(3)
    gbe.to_csv(OUT / "summary_be25.csv")

    glate = tr.groupby(["range_name", "model"]).apply(summarize).round(3)
    glate.to_csv(OUT / "summary_all_fills.csv")

    by_side = base.groupby(["range_name", "model", "side"]).apply(
        summarize, include_groups=False).round(3)
    by_side.to_csv(OUT / "by_side.csv")

    base2 = base.assign(year=pd.to_datetime(base["date"]).dt.year)
    by_year = base2.groupby(["range_name", "model", "year"])["r"].agg(
        ["count", "mean", "sum"]).round(3)
    by_year.to_csv(OUT / "by_year.csv")

    charts(base)
    report(tr, base, g, gbe, glate, by_side, by_year)
    print("done -> results/hourly_range_1m/")


def charts(base):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    fig, ax = plt.subplots(figsize=(9, 5))
    for (rng, model), d in base.groupby(["range_name", "model"]):
        d = d.sort_values("date")
        ax.plot(pd.to_datetime(d["date"]), d["r"].cumsum(), label=f"{rng} {model} (n={len(d)})")
    ax.set_ylabel("cumulative R")
    ax.set_title("Hourly-range fade models, base rules, pre-9:30 fills")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "equity_curves.png")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    t = base.groupby(["range_name", "model"])["outcome"].value_counts(normalize=True).unstack()
    t.plot.bar(ax=ax, rot=15)
    ax.axhline(1 / (1 + RR), color="r", ls="--", lw=1, label=f"breakeven win rate ({1/(1+RR):.1%})")
    ax.set_ylabel("share of trades")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "outcomes.png")
    plt.close("all")


def report(tr, base, g, gbe, glate, by_side, by_year):
    md = lambda t: t.to_markdown()
    lines = [
        "# Hourly-Range Fade Models on NQ 1m (6-7am and 7-8am ET candles)",
        "",
        f"{len(tr)} trades detected, {len(base)} with pre-9:30 fills (base case). "
        "Win = +1.3R, loss = -1R (stop = target distance / 1.3); EOD exits at the "
        "16:00 close. A bar that touches the midpoint and the level together "
        "counts as a cancel, and same-bar stop+target counts as stop "
        "(both conservative). Breakeven win rate at 1.3:1 is 43.5%.",
        "",
        "## Base rules, pre-9:30 fills",
        "", md(g), "",
        "## With breakeven stop once the 25% level trades",
        "", md(gbe), "",
        "## All fills allowed (entries any time before 16:00)",
        "", md(glate), "",
        "## By side (base)",
        "", md(by_side), "",
        "## By year (base)",
        "", md(by_year), "",
        "![equity](equity_curves.png)",
        "![outcomes](outcomes.png)",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
