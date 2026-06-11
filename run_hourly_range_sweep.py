"""Parameter sweep for the hourly-range fade models.

Adds to run_hourly_range.py:

* Break-quality features recorded per setup, used as entry filters:
    - penetration_pts : deepest excursion past the broken level during the
      break phase (wick), in points
    - penetration_atr : same, divided by the 1m ATR(14) at the break bar
    - disp_ratio      : breaking bar body / average body of the prior 20 bars
* Equilibrium zone: the target is pulled forward of the exact midpoint by
  zone_frac * range (0%, 5%, 10%). The stop is re-sized to keep 1.3:1 to the
  actual target, and the "no trade if target trades before the retest" cancel
  uses the zone edge, not the tick-perfect mid.

Filters skip the day's setup entirely (the model trades the FIRST qualifying
sequence; a filtered-out break does not roll to a later break).

Usage: python run_hourly_range_sweep.py [path-to-parquet]
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
OUT = Path("results/hourly_range_sweep")
OUT.mkdir(parents=True, exist_ok=True)

RANGES = {"6am": (6, 7), "7am": (7, 8)}
RR = 1.3
ENTRY_CUTOFF_MIN = 9 * 60 + 30
EOD_MIN = 16 * 60
ZONES = (0.0, 0.05, 0.10)            # target pulled forward of mid by zone*range

FILTERS = {
    "none":        lambda t: np.ones(len(t), bool),
    "pen>=5pts":   lambda t: t["penetration_pts"] >= 5,
    "pen>=10pts":  lambda t: t["penetration_pts"] >= 10,
    "pen>=15pts":  lambda t: t["penetration_pts"] >= 15,
    "pen>=0.5atr": lambda t: t["penetration_atr"] >= 0.5,
    "pen>=1atr":   lambda t: t["penetration_atr"] >= 1.0,
    "pen>=1.5atr": lambda t: t["penetration_atr"] >= 1.5,
    "disp>=1.5":   lambda t: t["disp_ratio"] >= 1.5,
}


def _resolve(h, l, c, mins, k, d, entry, stop, target, be_level):
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


def _break_info(o, h, l, c, atr, avg_body, rng_high, rng_low, model, d):
    """Locate the model's break sequence; return (seq_end, penetration, disp, atr_at)."""
    level = rng_high if d > 0 else rng_low
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
        pen = ((ext * d).max() - level * d)
        seq_end = back
    else:
        brkser = h if d > 0 else l
        brk = np.argmax(brkser * d > level * d) if ((brkser * d) > level * d).any() else -1
        if brk < 0:
            return None
        if not (rng_low <= c[brk] <= rng_high):
            return None
        pen = (brkser[brk] - level) * d
        seq_end = brk
    body = abs(c[brk] - o[brk])
    disp = body / avg_body[brk] if avg_body[brk] > 0 else np.nan
    a = atr[brk] if atr[brk] > 0 else np.nan
    return seq_end, pen, disp, pen / a if a and not np.isnan(a) else np.nan


def _hunt(h, l, c, mins, seq_end, d, level, tp, n):
    """Retest hunt with the cancel at the zone edge. Returns entry bar or -1/-2."""
    for k in range(seq_end + 1, n):
        tagged_tp = l[k] <= tp if d > 0 else h[k] >= tp
        retest = h[k] >= level if d > 0 else l[k] <= level
        if tagged_tp:
            return -2          # cancelled (zone edge first / ambiguous)
        if retest:
            return k
    return -1


def main():
    print("loading data...")
    df = load_continuous(DATA)
    df = df.assign(min=df.index.hour * 60 + df.index.minute)

    rows = []
    for date, day in df.groupby(df.index.date):
        for range_name, (h0, h1) in RANGES.items():
            rng = day[(day["min"] >= h0 * 60) & (day["min"] < h1 * 60)]
            if len(rng) < 50:
                continue
            rng_high, rng_low = rng["high"].max(), rng["low"].min()
            width = rng_high - rng_low
            mid = (rng_high + rng_low) / 2
            bars = day[(day["min"] >= h1 * 60) & (day["min"] < EOD_MIN)]
            if len(bars) < 30:
                continue
            # feature context: include two prior hours so ATR/body baselines exist
            ctx = day[(day["min"] >= (h1 - 2) * 60) & (day["min"] < EOD_MIN)]
            o = ctx["open"].to_numpy()
            h = ctx["high"].to_numpy()
            l = ctx["low"].to_numpy()
            c = ctx["close"].to_numpy()
            mins = ctx["min"].to_numpy()
            off = int(np.argmax(mins >= h1 * 60))
            tr_ = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
            tr_[0] = h[0] - l[0]
            atr = pd.Series(tr_).rolling(14, min_periods=5).mean().to_numpy()
            avg_body = pd.Series(abs(c - o)).rolling(20, min_periods=10).mean().shift(1).to_numpy()
            ho, hh, hl, hc, hm = o[off:], h[off:], l[off:], c[off:], mins[off:]
            n = len(hc)

            for model in ("retest", "failure"):
                for d in (+1, -1):
                    info = _break_info(ho, hh, hl, hc, atr[off:], avg_body[off:],
                                       rng_high, rng_low, model, d)
                    if info is None:
                        continue
                    seq_end, pen, disp, pen_atr = info
                    level = rng_high if d > 0 else rng_low
                    for zone in ZONES:
                        tp = mid + d * zone * width
                        tp_d = (level - tp) * d
                        if tp_d <= 0:
                            continue
                        k = _hunt(hh, hl, hc, hm, seq_end, d, level, tp, n)
                        if k < 0:
                            continue
                        if hm[k] >= ENTRY_CUTOFF_MIN:
                            continue   # base case: pre-9:30 fills only
                        entry = level
                        stop = entry + d * tp_d / RR
                        be_level = (entry + tp) / 2
                        outcome, r, r_be, exit_min = _resolve(
                            hh, hl, hc, hm, k, d, entry, stop, tp, be_level)
                        rows.append({
                            "date": date, "range_name": range_name, "model": model,
                            "side": "short" if d > 0 else "long", "zone": zone,
                            "range_width": width, "penetration_pts": pen,
                            "penetration_atr": pen_atr, "disp_ratio": disp,
                            "stop_pts": tp_d / RR, "entry_minute": int(hm[k]),
                            "outcome": outcome, "r": r, "r_be25": r_be,
                            "minutes_to_exit": exit_min - hm[k]})

    tr = pd.DataFrame(rows)
    tr.to_csv(OUT / "trades_sweep.csv", index=False)
    print(f"{len(tr)} trade-variants ({tr[tr['zone'] == 0].shape[0]} at zone=0)")

    # --- sweep grid ----------------------------------------------------------
    grid_rows = []
    for (range_name, model, zone), d in tr.groupby(["range_name", "model", "zone"]):
        for fname, fn in FILTERS.items():
            sel = d[fn(d).fillna(False) if hasattr(fn(d), "fillna") else fn(d)]
            if len(sel) < 50:
                continue
            for rcol, tag in [("r", "fixed"), ("r_be25", "be25")]:
                r = sel[rcol]
                grid_rows.append({
                    "range": range_name, "model": model, "zone": zone,
                    "filter": fname, "mgmt": tag, "n": len(sel),
                    "win_rate": (sel["outcome"] == "win").mean(),
                    "avg_R": r.mean(), "total_R": r.sum(),
                    "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
                })
    grid = pd.DataFrame(grid_rows).round(3)
    grid.to_csv(OUT / "grid.csv", index=False)

    # --- best cells, with yearly stability ----------------------------------
    fixed = grid[grid["mgmt"] == "fixed"].sort_values("avg_R", ascending=False)
    best = fixed.head(12)
    best.to_csv(OUT / "best_cells.csv", index=False)

    year_rows = []
    for _, b in best.head(5).iterrows():
        d = tr[(tr["range_name"] == b["range"]) & (tr["model"] == b["model"])
               & (tr["zone"] == b["zone"])]
        d = d[FILTERS[b["filter"]](d).fillna(False)]
        d = d.assign(year=pd.to_datetime(d["date"]).dt.year)
        y = d.groupby("year")["r"].agg(["count", "mean", "sum"]).round(3)
        y.insert(0, "cell", f"{b['range']}/{b['model']}/z{b['zone']}/{b['filter']}")
        year_rows.append(y)
    pd.concat(year_rows).to_csv(OUT / "best_by_year.csv")

    charts(tr, grid)
    report(tr, grid, best)
    print(f"done -> {OUT}/")


def charts(tr, grid):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": False})
    fixed = grid[grid["mgmt"] == "fixed"]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    cells = [("6am", "retest"), ("6am", "failure"), ("7am", "retest"), ("7am", "failure")]
    for ax, (rng, model) in zip(axes.flat, cells):
        t = fixed[(fixed["range"] == rng) & (fixed["model"] == model)]
        piv = t.pivot(index="filter", columns="zone", values="avg_R").reindex(list(FILTERS))
        im = ax.imshow(piv.to_numpy(), cmap="RdYlGn", vmin=-0.15, vmax=0.15, aspect="auto")
        ax.set_xticks(range(len(piv.columns)), [f"zone {z:.0%}" for z in piv.columns])
        ax.set_yticks(range(len(piv.index)), piv.index)
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                v = piv.iloc[i, j]
                if not np.isnan(v):
                    ax.text(j, i, f"{v:+.3f}", ha="center", va="center", fontsize=8)
        ax.set_title(f"{rng} {model}")
    fig.colorbar(im, ax=axes, shrink=0.6, label="avg R")
    fig.suptitle("Avg R by break filter and equilibrium zone (fixed stop, pre-9:30 fills)")
    fig.savefig(OUT / "sweep_heatmap.png", bbox_inches="tight")
    plt.close("all")


def report(tr, grid, best):
    md = lambda t: t.to_markdown(index=False)
    base = tr[tr["zone"] == 0]
    lines = [
        "# Hourly-Range Fade: break-quality filters and equilibrium-zone targets",
        "",
        f"{len(base)} base setups per zone variant. Penetration = deepest wick past "
        "the broken level during the break phase; ATR = 1m ATR(14) at the break "
        "bar; displacement = breaking bar body / prior 20-bar average body. "
        "Zone z pulls the target forward of the midpoint by z*range; the stop is "
        "re-sized to keep 1.3:1 to the actual target and the cancel rule applies "
        "at the zone edge. Filters skip the day (no roll to a later break). "
        "Cells with n < 50 omitted.",
        "",
        f"Setup penetration distribution: median "
        f"{base['penetration_pts'].median():.1f} pts "
        f"({base['penetration_atr'].median():.2f} ATR); 25th pct "
        f"{base['penetration_pts'].quantile(.25):.1f} pts — the user's read is "
        "correct that most breaks barely clear the level.",
        "",
        "## Best cells (fixed stop management)",
        "", md(best), "",
        "![heatmap](sweep_heatmap.png)",
        "",
        "## Full grid",
        "", md(grid[grid["mgmt"] == "fixed"]), "",
        "## Full grid (breakeven-at-halfway management)",
        "", md(grid[grid["mgmt"] == "be25"]), "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
