"""Exit-structure variants for the hourly-range fade models.

Same setup detection as run_hourly_range_sweep.py (zone=0, pre-9:30 fills,
midpoint cancel rule). Each setup is then run through every exit config:

Stops:
  rr1.3    : formula stop, distance = target distance / 1.3 (singles only)
  extreme  : behind the break extreme (the penetration wick), min 1 tick
  proj25   : level +/- 0.25 * range beyond the broken level
  proj50   : level +/- 0.50 * range beyond the broken level

Single targets: mid (50% retrace) or opposite end of the range.

Partials (50/50, stop to breakeven after the first leg fills, BE effective
the NEXT bar -- conservative):
  quad_25_50 : first leg at the 25% retrace, second at the mid
  mid_opp    : first leg at the mid, second at the opposite end
  r1_r2      : first leg at +1R (one stop-distance), second at +2R

R is normalized by the initial stop distance. Same-bar stop+target counts as
stop. Remaining size exits at the 16:00 close.

Usage: python run_hourly_range_variants.py [path-to-parquet]
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous
from run_hourly_range_sweep import _break_info, _hunt, RANGES, ENTRY_CUTOFF_MIN, EOD_MIN

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
OUT = Path("results/hourly_range_variants")
OUT.mkdir(parents=True, exist_ok=True)

MIN_STOP = 0.25  # 1 tick


def _sim(h, l, c, k, n, d, entry, stop0, legs, be_after_first):
    """legs: list of (fraction, target_px), nearest first. Returns R or nan."""
    stop_d = (stop0 - entry) * d
    if stop_d < MIN_STOP:
        return np.nan
    stop = stop0
    filled = [False] * len(legs)
    pnl = 0.0
    open_frac = 1.0
    be_pending = False
    for m in range(k, n):
        if be_pending:
            stop = entry
            be_pending = False
        if (h[m] >= stop if d > 0 else l[m] <= stop):
            pnl += open_frac * (entry - stop) * d
            open_frac = 0.0
            break
        for i, (frac, tgt) in enumerate(legs):
            if not filled[i] and (l[m] <= tgt if d > 0 else h[m] >= tgt):
                filled[i] = True
                pnl += frac * (entry - tgt) * d
                open_frac -= frac
                if i == 0 and be_after_first:
                    be_pending = True
        if open_frac <= 1e-9:
            break
    if open_frac > 1e-9:
        pnl += open_frac * (entry - c[n - 1]) * d
    return pnl / stop_d


def configs(level, mid, opp, width, pen, d):
    """Build {name: (stop_px, legs, be)} for one setup."""
    q25 = level - d * 0.25 * width          # first quadrant retrace
    r_stop = {
        "extreme": level + d * max(pen, MIN_STOP),
        "proj25":  level + d * 0.25 * width,
        "proj50":  level + d * 0.50 * width,
    }
    out = {}
    for tname, tpx in [("mid", mid), ("opp", opp)]:
        out[f"rr1.3->{tname}"] = (level + d * abs(level - tpx) / 1.3, [(1.0, tpx)], False)
        for sname, spx in r_stop.items():
            out[f"{sname}->{tname}"] = (spx, [(1.0, tpx)], False)
    for sname, spx in r_stop.items():
        sd = (spx - level) * d
        out[f"{sname} 50@q25/50@mid"] = (spx, [(0.5, q25), (0.5, mid)], True)
        out[f"{sname} 50@mid/50@opp"] = (spx, [(0.5, mid), (0.5, opp)], True)
        out[f"{sname} 50@1R/50@2R"] = (spx, [(0.5, level - d * sd), (0.5, level - d * 2 * sd)], True)
    return out


def main():
    print("loading data...")
    df = load_continuous(DATA)
    df = df.assign(min=df.index.hour * 60 + df.index.minute)

    rows = []
    cfg_names = None
    for date, day in df.groupby(df.index.date):
        for range_name, (h0, h1) in RANGES.items():
            rng = day[(day["min"] >= h0 * 60) & (day["min"] < h1 * 60)]
            if len(rng) < 50:
                continue
            rng_high, rng_low = rng["high"].max(), rng["low"].min()
            width = rng_high - rng_low
            mid = (rng_high + rng_low) / 2
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
            if n < 30:
                continue

            for model in ("retest", "failure"):
                for d in (+1, -1):
                    info = _break_info(ho, hh, hl, hc, atr[off:], avg_body[off:],
                                       rng_high, rng_low, model, d)
                    if info is None:
                        continue
                    seq_end, pen, disp, pen_atr = info
                    level = rng_high if d > 0 else rng_low
                    opp = rng_low if d > 0 else rng_high
                    k = _hunt(hh, hl, hc, hm, seq_end, d, level, mid, n)
                    if k < 0 or hm[k] >= ENTRY_CUTOFF_MIN:
                        continue
                    row = {"date": date, "range_name": range_name, "model": model,
                           "side": "short" if d > 0 else "long",
                           "penetration_pts": pen, "penetration_atr": pen_atr,
                           "disp_ratio": disp, "range_width": width}
                    cfgs = configs(level, mid, opp, width, pen, d)
                    cfg_names = list(cfgs)
                    for name, (spx, legs, be) in cfgs.items():
                        row[name] = _sim(hh, hl, hc, k, n, d, level, spx, legs, be)
                    rows.append(row)

    tr = pd.DataFrame(rows)
    tr.to_csv(OUT / "variants.csv", index=False)
    print(f"{len(tr)} setups x {len(cfg_names)} configs")

    pops = {"all": tr, "pen>=0.5atr": tr[tr["penetration_atr"] >= 0.5]}
    tabs = {}
    for pop_name, d in pops.items():
        recs = []
        for (range_name, model), g in d.groupby(["range_name", "model"]):
            for cfg in cfg_names:
                r = g[cfg].dropna()
                if len(r) < 40:
                    continue
                recs.append({
                    "range": range_name, "model": model, "config": cfg, "n": len(r),
                    "win_rate": (r > 0).mean(), "avg_R": r.mean(),
                    "median_R": r.median(), "total_R": r.sum(),
                    "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
                })
        tabs[pop_name] = pd.DataFrame(recs).round(3)
        tabs[pop_name].to_csv(OUT / f"summary_{pop_name.replace('>=', '_ge_')}.csv", index=False)

    # yearly stability for the filtered population's top cells
    top = tabs["pen>=0.5atr"].sort_values("avg_R", ascending=False).head(8)
    top.to_csv(OUT / "top_filtered.csv", index=False)
    yr_rows = []
    d = pops["pen>=0.5atr"].assign(year=pd.to_datetime(pops["pen>=0.5atr"]["date"]).dt.year)
    for _, b in top.head(4).iterrows():
        g = d[(d["range_name"] == b["range"]) & (d["model"] == b["model"])]
        y = g.groupby("year")[b["config"]].agg(["count", "mean", "sum"]).round(3)
        y.insert(0, "cell", f"{b['range']}/{b['model']}/{b['config']}")
        yr_rows.append(y)
    pd.concat(yr_rows).to_csv(OUT / "top_by_year.csv")

    charts(tabs, cfg_names)
    report(tr, tabs, top)
    print(f"done -> {OUT}/")


def charts(tabs, cfg_names):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": False})
    for pop_name, t in tabs.items():
        cells = sorted(set(zip(t["range"], t["model"])))
        piv = pd.DataFrame(index=cfg_names,
                           columns=[f"{r} {m}" for r, m in cells], dtype=float)
        for _, row in t.iterrows():
            piv.loc[row["config"], f"{row['range']} {row['model']}"] = row["avg_R"]
        fig, ax = plt.subplots(figsize=(8, 0.45 * len(cfg_names) + 1.5))
        im = ax.imshow(piv.to_numpy(), cmap="RdYlGn", vmin=-0.25, vmax=0.25, aspect="auto")
        ax.set_xticks(range(len(piv.columns)), piv.columns, rotation=15)
        ax.set_yticks(range(len(piv.index)), piv.index, fontsize=8)
        for i in range(piv.shape[0]):
            for j in range(piv.shape[1]):
                v = piv.iloc[i, j]
                if not np.isnan(v):
                    ax.text(j, i, f"{v:+.2f}", ha="center", va="center", fontsize=7)
        ax.set_title(f"avg R by exit config ({pop_name})")
        fig.colorbar(im, ax=ax, shrink=0.7)
        fig.tight_layout()
        fig.savefig(OUT / f"heatmap_{pop_name.replace('>=', '_ge_')}.png")
    plt.close("all")


def report(tr, tabs, top):
    md = lambda t: t.to_markdown(index=False)
    lines = [
        "# Hourly-Range Fade: stop / target / partial variants",
        "",
        f"{len(tr)} setups (zone=0 detection, pre-9:30 fills, midpoint cancel "
        "rule). R normalized by initial stop distance; same-bar stop+target = "
        "stop; breakeven moves apply from the bar after the first partial fills; "
        "remaining size exits on the 16:00 close. Cells with n < 40 omitted.",
        "",
        "## Top configs, penetration >= 0.5 ATR population",
        "", md(top), "",
        "![heatmap filtered](heatmap_pen_ge_0.5atr.png)",
        "",
        "## All setups (no break filter)",
        "", md(tabs["all"].sort_values("avg_R", ascending=False).head(15)), "",
        "![heatmap all](heatmap_all.png)",
        "",
        "Full tables: summary_all.csv, summary_pen_ge_0.5atr.csv; yearly "
        "stability of the top cells in top_by_year.csv.",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
