"""Run the full IB probability study on NQ 1m data and write results/.

Usage: python run_backtest.py [path-to-parquet]
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous, rth_days
from ib_engine.events import analyze_day, events_to_frame, EXTENSIONS
from ib_engine.probability import add_buckets, conditional_table, strategy_summary, wilson_halfwidth

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
OUT = Path("results")
OUT.mkdir(exist_ok=True)


def main():
    print("loading data...")
    df = load_continuous(DATA)
    print(f"continuous series: {len(df):,} bars  {df.index[0]} -> {df.index[-1]}")

    day_recs, all_events = [], []
    prev_close = None
    for date, ib, post in rth_days(df):
        rec, evs = analyze_day(date, ib, post, prev_close)
        day_recs.append(rec)
        all_events.extend(evs)
        prev_close = post["close"].iloc[-1]

    days = pd.DataFrame([vars(r) for r in day_recs])
    ev = events_to_frame(all_events)
    print(f"sessions analyzed: {len(days)},  breakout events: {len(ev)}")

    # --- context joins ----------------------------------------------------
    days["ib_range_pctile"] = days["ib_range"].rolling(60, min_periods=20).rank(pct=True)
    days["prev_close"] = days["ib_open"] - days["gap_points"]
    days["gap_pct"] = days["gap_points"] / days["prev_close"]
    days["gap_dir"] = np.select([days["gap_pct"] > 0.001, days["gap_pct"] < -0.001],
                                ["gap_up", "gap_down"], default="flat")
    ctx = days.set_index("date")[["ib_range_pctile", "gap_dir"]]
    ev["ib_range_pctile"] = ev["date"].map(ctx["ib_range_pctile"])
    ev["gap_dir"] = ev["date"].map(ctx["gap_dir"])
    ev = ev.dropna(subset=["ib_range_pctile", "disp_ratio"])
    ev = add_buckets(ev)
    ev.rename(columns={f"ext_{x}": f"ext_{x}" for x in EXTENSIONS}, inplace=True)

    days.to_csv(OUT / "days.csv", index=False)
    ev.to_csv(OUT / "breakout_events.csv", index=False)

    first = ev[ev["is_first"]].copy()

    # --- probability tables -------------------------------------------------
    tables = {}
    tables["day_patterns"] = (days["pattern"].value_counts(normalize=True).rename("prob")
                              .to_frame().assign(n=days["pattern"].value_counts()).round(3))
    yearly = days.assign(year=pd.to_datetime(days["date"]).dt.year)
    tables["day_patterns_by_year"] = (yearly.groupby("year")["pattern"]
                                      .value_counts(normalize=True).unstack().round(3))

    base = first[[f"ext_{x}" for x in EXTENSIONS] +
                 ["failed", "failed_before_05", "cisd", "retest", "resumed_after_retest",
                  "reached_opposite_after", "eod_beyond_edge"]].mean().rename("prob").to_frame()
    base["n"] = len(first)
    base["ci95"] = wilson_halfwidth(base["prob"].values, len(first))
    tables["baseline_first_breakout"] = base.round(3)

    tables["by_direction"] = conditional_table(first, "direction")
    tables["by_displacement"] = conditional_table(first, "disp_q")
    tables["by_close_through"] = conditional_table(first, "close_through_b")
    tables["by_fvg"] = conditional_table(first, "fvg_present")
    tables["by_time_of_day"] = conditional_table(first, "time_b")
    tables["by_ib_width"] = conditional_table(first, "ib_width_q")
    tables["by_gap"] = conditional_table(first, "gap_dir")
    tables["by_dow"] = conditional_table(first, "dow_name")
    tables["by_opposite_touched_first"] = conditional_table(first, "opposite_touched_first")

    # CISD deep-dive
    cisd = first[first["cisd"]]
    tables["cisd_outcomes"] = pd.DataFrame({
        "prob": {
            "P(CISD | breakout)": first["cisd"].mean(),
            "P(reach IB mid | CISD)": cisd["cisd_then_mid"].mean(),
            "P(reach opposite side | CISD)": cisd["cisd_then_opposite"].mean(),
            "P(reach opposite side | no CISD)": first[~first["cisd"]]["reached_opposite_after"].mean(),
            "P(reach opposite side | any breakout)": first["reached_opposite_after"].mean(),
        },
        "n": {"P(CISD | breakout)": len(first), "P(reach IB mid | CISD)": len(cisd),
              "P(reach opposite side | CISD)": len(cisd),
              "P(reach opposite side | no CISD)": int((~first["cisd"]).sum()),
              "P(reach opposite side | any breakout)": len(first)},
    }).round(3)
    tables["cisd_by_displacement"] = conditional_table(
        first, "disp_q", ["cisd", "cisd_then_mid", "cisd_then_opposite"])

    # FVG deep-dive
    fvg = first[first["fvg_present"]]
    tables["fvg_outcomes"] = pd.DataFrame({
        "prob": {
            "P(FVG forms at breakout)": first["fvg_present"].mean(),
            "P(price revisits FVG | FVG)": fvg["fvg_revisited"].mean(),
            "P(FVG holds -> 0.5R ext | revisited)":
                fvg[fvg["fvg_revisited"]]["fvg_held"].mean(),
            "P(reach 1R ext | FVG)": fvg["ext_1.0"].mean(),
            "P(reach 1R ext | no FVG)": first[~first["fvg_present"]]["ext_1.0"].mean(),
            "P(failed | FVG)": fvg["failed"].mean(),
            "P(failed | no FVG)": first[~first["fvg_present"]]["failed"].mean(),
        },
        "n": {"P(FVG forms at breakout)": len(first),
              "P(price revisits FVG | FVG)": len(fvg),
              "P(FVG holds -> 0.5R ext | revisited)": int(fvg["fvg_revisited"].sum()),
              "P(reach 1R ext | FVG)": len(fvg),
              "P(reach 1R ext | no FVG)": int((~first["fvg_present"]).sum()),
              "P(failed | FVG)": len(fvg),
              "P(failed | no FVG)": int((~first["fvg_present"]).sum())},
    }).round(3)

    # joint condition: displacement x FVG
    tables["disp_x_fvg"] = (first.groupby(["disp_q", "fvg_present"], observed=True)
                            [["ext_0.5", "ext_1.0", "failed", "reached_opposite_after"]]
                            .agg(["mean", "size"]).round(3))

    tables["strategies"] = strategy_summary(ev)

    for name, t in tables.items():
        t.to_csv(OUT / f"{name}.csv")

    charts(days, first, ev)
    write_report(days, ev, first, tables)
    print("done -> results/")


def charts(days, first, ev):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})

    # 1. extension reach curve
    fig, ax = plt.subplots(figsize=(7, 4.5))
    xs = list(EXTENSIONS)
    for d, g in first.groupby("direction"):
        ax.plot(xs, [g[f"ext_{x}"].mean() for x in xs], marker="o", label=f"{d} breakout (n={len(g)})")
    ax.plot(xs, [first[f"ext_{x}"].mean() for x in xs], marker="s", color="k", ls="--",
            label=f"all (n={len(first)})")
    ax.set_xlabel("extension beyond IB edge (x IB range)")
    ax.set_ylabel("P(reached before EOD)")
    ax.set_title("How far do IB breakouts run?")
    ax.legend()
    fig.tight_layout()
    fig.savefig("results/extension_curve.png")

    # 2. outcomes by displacement quartile
    fig, ax = plt.subplots(figsize=(7, 4.5))
    t = first.groupby("disp_q", observed=True)[["ext_0.5", "ext_1.0", "failed", "reached_opposite_after"]].mean()
    t.plot.bar(ax=ax, rot=0)
    ax.set_ylabel("probability")
    ax.set_title("Breakout outcomes by displacement strength")
    fig.tight_layout()
    fig.savefig("results/by_displacement.png")

    # 3. outcomes by time of day
    fig, ax = plt.subplots(figsize=(7, 4.5))
    t = first.groupby("time_b", observed=True)[["ext_1.0", "failed", "reached_opposite_after"]].mean()
    t.plot.bar(ax=ax, rot=0)
    ax.set_ylabel("probability")
    ax.set_title("Breakout outcomes by time of breakout")
    fig.tight_layout()
    fig.savefig("results/by_time.png")

    # 4. day pattern mix by year
    fig, ax = plt.subplots(figsize=(7, 4.5))
    yearly = days.assign(year=pd.to_datetime(days["date"]).dt.year)
    t = yearly.groupby("year")["pattern"].value_counts(normalize=True).unstack()
    t.plot.bar(stacked=True, ax=ax, rot=0)
    ax.set_ylabel("share of sessions")
    ax.set_title("Day-type mix by year")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig("results/day_mix.png")

    # 5. MFE distribution
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(first["mfe"].clip(upper=4), bins=60)
    ax.set_xlabel("max favorable excursion beyond edge (x IB range, clipped at 4)")
    ax.set_ylabel("breakouts")
    ax.set_title("MFE distribution of first IB breakouts")
    fig.tight_layout()
    fig.savefig("results/mfe_hist.png")
    plt.close("all")


def write_report(days, ev, first, tables):
    def md(t):
        return t.to_markdown()

    yrs = pd.to_datetime(days["date"]).dt.year
    lines = [
        "# NQ Initial Balance Probability Study",
        "",
        f"**Data:** NQ futures 1m, volume-rolled front month, {days['date'].iloc[0]} to "
        f"{days['date'].iloc[-1]} ({len(days)} regular sessions, {len(first)} first-breakout events).",
        "",
        "**IB definition:** 9:30-10:30 ET high/low. All distances in *R* = IB range "
        "(high minus low). A *breakout* is the first 1-minute close beyond an IB edge after "
        "10:30 ET. *Displacement* = breakout-bar body / average body of the prior 20 bars. "
        "*FVG* = 3-candle imbalance near the breakout bar. *CISD* = a later close through the "
        "open of the candle sequence that delivered the breakout. Strategy sims resolve "
        "same-bar stop/target ambiguity against the trade.",
        "",
        "## 1. Day taxonomy",
        "", md(tables["day_patterns"]), "",
        "By year:", "", md(tables["day_patterns_by_year"]), "",
        "![day mix](day_mix.png)",
        "",
        "## 2. Baseline: what happens after the first breakout",
        "", md(tables["baseline_first_breakout"]), "",
        "![extension curve](extension_curve.png)",
        "![mfe](mfe_hist.png)",
        "",
        "## 3. Conditional probabilities",
        "",
        "### By direction", "", md(tables["by_direction"]), "",
        "### By displacement strength", "", md(tables["by_displacement"]), "",
        "![displacement](by_displacement.png)", "",
        "### By close-through depth", "", md(tables["by_close_through"]), "",
        "### FVG present at breakout", "", md(tables["by_fvg"]), "",
        "### By time of breakout", "", md(tables["by_time_of_day"]), "",
        "![time](by_time.png)", "",
        "### By IB width (60-day percentile, quintiles)", "", md(tables["by_ib_width"]), "",
        "### By overnight gap", "", md(tables["by_gap"]), "",
        "### By day of week", "", md(tables["by_dow"]), "",
        "### Opposite side touched before the breakout", "",
        md(tables["by_opposite_touched_first"]), "",
        "## 4. CISD (failed-breakout reversal)",
        "", md(tables["cisd_outcomes"]), "",
        "By displacement of the original breakout (joint probabilities, i.e. "
        "P(CISD and outcome | breakout)):", "", md(tables["cisd_by_displacement"]), "",
        "## 5. Fair value gaps",
        "", md(tables["fvg_outcomes"]), "",
        "### Displacement x FVG joint table", "", md(tables["disp_x_fvg"]), "",
        "## 6. Strategy expectancies (R-multiples)",
        "", md(tables["strategies"]), "",
        "*S1 = market entry on breakout close, stop IB mid, target 1R extension. "
        "S2 = limit at IB edge on retest, stop IB mid. "
        "S3 = fade on CISD close, stop at post-breakout extreme, target opposite edge. "
        "EOD flat exit applies to all.*",
    ]
    Path("results/REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
