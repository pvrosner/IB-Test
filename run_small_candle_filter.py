"""Apply the 'small candles revert' filter to the actual fade trades.

Joins the hourly-range fade trades (sweep + exit variants) with each
session's candle character (relative range = candle range / trailing 20-day
median of the same hour slot, known in real time at candle close) and
measures expectancy by relative-range tercile, alone and combined with the
penetration filter and the best exit structures.

Usage: python run_small_candle_filter.py
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

OUT = Path("results/small_candle_filter")
OUT.mkdir(parents=True, exist_ok=True)

SLOT = {"6am": 6, "7am": 7}


def load():
    cand = pd.read_csv("results/candle_character/candles.csv")[["date", "slot", "rel_range"]]
    tr = pd.read_csv("results/hourly_range_sweep/trades_sweep.csv")
    tr = tr[tr["zone"] == 0].copy()
    tr["slot"] = tr["range_name"].map(SLOT)
    tr = tr.merge(cand, on=["date", "slot"], how="left").dropna(subset=["rel_range"])
    tr["size_b"] = tr.groupby("slot")["rel_range"].transform(
        lambda s: pd.qcut(s, 3, labels=["small", "mid", "large"]))

    va = pd.read_csv("results/hourly_range_variants/variants.csv")
    va["slot"] = va["range_name"].map(SLOT)
    va = va.merge(cand, on=["date", "slot"], how="left").dropna(subset=["rel_range"])
    va["size_b"] = va.groupby("slot")["rel_range"].transform(
        lambda s: pd.qcut(s, 3, labels=["small", "mid", "large"]))
    return tr, va


def summ(r, outcome=None):
    r = r.dropna()
    if len(r) < 30:
        return None
    return {
        "n": len(r), "win_rate": (r > 0).mean(), "avg_R": r.mean(),
        "total_R": r.sum(),
        "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
    }


def main():
    tr, va = load()
    print(f"{len(tr)} base trades joined with candle character")

    # 1. base model expectancy by candle size
    rows = []
    for (rng, model, size), d in tr.groupby(["range_name", "model", "size_b"], observed=True):
        s = summ(d["r"])
        if s:
            rows.append({"range": rng, "model": model, "size": size, **s})
    t1 = pd.DataFrame(rows).round(3)
    t1.to_csv(OUT / "base_by_size.csv", index=False)

    # pooled across models/ranges
    rows = []
    for size, d in tr.groupby("size_b", observed=True):
        s = summ(d["r"])
        rows.append({"size": size, **s})
    t1p = pd.DataFrame(rows).round(3)
    t1p.to_csv(OUT / "base_by_size_pooled.csv", index=False)

    # 2. interaction with the penetration filter
    rows = []
    for size, d in tr.groupby("size_b", observed=True):
        for pname, sel in [("no pen filter", d),
                           ("pen>=0.5atr", d[d["penetration_atr"] >= 0.5])]:
            s = summ(sel["r"])
            if s:
                rows.append({"size": size, "pen_filter": pname, **s})
    t2 = pd.DataFrame(rows).round(3)
    t2.to_csv(OUT / "size_x_penetration.csv", index=False)

    # 3. best exit variants under the size filter
    cfgs = ["rr1.3->mid", "rr1.3->opp", "proj25->mid", "proj25->opp",
            "extreme 50@1R/50@2R", "proj25 50@mid/50@opp"]
    rows = []
    for size, d in va.groupby("size_b", observed=True):
        for pname, sel in [("all", d), ("pen>=0.5atr", d[d["penetration_atr"] >= 0.5])]:
            for cfg in cfgs:
                s = summ(sel[cfg])
                if s:
                    rows.append({"size": size, "pen_filter": pname, "config": cfg, **s})
    t3 = pd.DataFrame(rows).round(3)
    t3.to_csv(OUT / "variants_by_size.csv", index=False)

    # 3b. drill-down: the failure-model winning cells by size
    rows = []
    fail = va[(va["model"] == "failure") & (va["penetration_atr"] >= 0.5)]
    for scope, d in [("7am only", fail[fail["range_name"] == "7am"]), ("both ranges", fail)]:
        for cfg in ["rr1.3->mid", "extreme 50@1R/50@2R", "proj25->opp"]:
            for size, g in d.groupby("size_b", observed=True):
                r = g[cfg].dropna()
                if len(r) < 30:
                    continue
                rows.append({"scope": scope, "config": cfg, "size": size, "n": len(r),
                             "win_rate": (r > 0).mean(), "avg_R": r.mean(),
                             "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum())})
    t3b = pd.DataFrame(rows).round(3)
    t3b.to_csv(OUT / "failure_cells_by_size.csv", index=False)

    # 4. yearly stability of the headline cells
    tr["year"] = pd.to_datetime(tr["date"]).dt.year
    va["year"] = pd.to_datetime(va["date"]).dt.year
    y1 = (tr[tr["size_b"] == "small"].groupby("year")["r"]
          .agg(["count", "mean", "sum"]).round(3))
    y1.insert(0, "cell", "small candles, base model (rr1.3->mid)")
    best = va[(va["size_b"] == "small")]
    y2 = best.groupby("year")["extreme 50@1R/50@2R"].agg(["count", "mean", "sum"]).round(3)
    y2.insert(0, "cell", "small candles, extreme 50@1R/50@2R")
    pd.concat([y1, y2]).to_csv(OUT / "by_year.csv")

    # 5. equity curves
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    fig, ax = plt.subplots(figsize=(9, 5))
    for size, d in tr.groupby("size_b", observed=True):
        d = d.sort_values("date")
        ax.plot(pd.to_datetime(d["date"]), d["r"].cumsum(),
                label=f"{size} candles (n={len(d)}, avg {d['r'].mean():+.3f}R)")
    ax.set_ylabel("cumulative R (base model, rr 1.3 -> mid)")
    ax.set_title("Hourly-range fades split by candle size (relative range terciles)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "equity_by_size.png")
    plt.close("all")

    md = lambda t: t.to_markdown(index=False)
    lines = [
        "# 'Small Candles Revert' as a Trade Filter",
        "",
        "Relative range = candle range / trailing 20-day median of the same hour "
        "slot (computable in real time at candle close). Terciles per slot. "
        "Trades are the actual fade entries from the hourly-range studies "
        "(pre-9:30 fills, midpoint cancel rule).",
        "",
        "## Base model (rr 1.3 -> mid) by candle size, pooled",
        "", md(t1p), "",
        "## By range and model",
        "", md(t1), "",
        "## Candle size x penetration filter",
        "", md(t2), "",
        "## Exit variants under the size filter",
        "", md(t3.sort_values('avg_R', ascending=False).head(20)), "",
        "## Drill-down: failure model + pen>=0.5atr, by candle size",
        "", md(t3b), "",
        "## Yearly stability (headline cells)",
        "", pd.read_csv(OUT / 'by_year.csv').to_markdown(index=False), "",
        "![equity](equity_by_size.png)",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))
    print("done -> results/small_candle_filter/")


if __name__ == "__main__":
    main()
