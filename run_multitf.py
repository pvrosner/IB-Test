"""Re-run the IB study on 1m, 5m and 15m bars and compare.

The timeframe changes what counts as an event: a breakout is the first
*close of that timeframe* beyond the IB edge, displacement is measured
against the prior 20 bars of that timeframe, FVGs are 3-candle imbalances
of that timeframe, and CISD is a close of that timeframe through the
impulse origin. The IB itself (9:30-10:30 ET high/low) is identical.

Usage: python run_multitf.py [path-to-parquet]
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous, resample_tf, rth_days
from ib_engine.events import analyze_day, events_to_frame, EXTENSIONS
from ib_engine.probability import add_buckets, strategy_summary

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
OUT = Path("results/multitf")
OUT.mkdir(parents=True, exist_ok=True)

TFS = [1, 5, 15]

BASE_OUTCOMES = [f"ext_{x}" for x in EXTENSIONS] + [
    "failed", "failed_before_05", "cisd", "retest", "resumed_after_retest",
    "reached_opposite_after", "eod_beyond_edge"]


def run_tf(df1m: pd.DataFrame, tf: int):
    df = resample_tf(df1m, tf)
    day_recs, all_events = [], []
    prev_close = None
    for date, ib, post in rth_days(df, tf):
        rec, evs = analyze_day(date, ib, post, prev_close)
        day_recs.append(rec)
        all_events.extend(evs)
        prev_close = post["close"].iloc[-1]

    days = pd.DataFrame([vars(r) for r in day_recs])
    ev = events_to_frame(all_events)
    # convert bar counts to clock minutes
    for col in ["minutes_after_ib", "retest_minutes", "failed_minutes", "cisd_minutes"]:
        ev[col] = ev[col] * tf
    days["ib_range_pctile"] = days["ib_range"].rolling(60, min_periods=20).rank(pct=True)
    ev["ib_range_pctile"] = ev["date"].map(days.set_index("date")["ib_range_pctile"])
    ev = ev.dropna(subset=["ib_range_pctile", "disp_ratio"])
    ev = add_buckets(ev)
    return days, ev


def main():
    print("loading data...")
    df1m = load_continuous(DATA)

    results = {}
    for tf in TFS:
        days, ev = run_tf(df1m, tf)
        results[tf] = (days, ev)
        ev.to_csv(OUT / f"breakout_events_{tf}m.csv", index=False)
        print(f"{tf}m: {len(days)} sessions, {ev['is_first'].sum()} first breakouts")

    cols = {tf: f"{tf}m" for tf in TFS}

    # --- baseline comparison ----------------------------------------------
    base = pd.DataFrame({
        cols[tf]: results[tf][1].loc[results[tf][1]["is_first"], BASE_OUTCOMES].mean()
        for tf in TFS})
    n_row = pd.DataFrame({cols[tf]: {"n_first_breakouts": int(results[tf][1]["is_first"].sum())}
                          for tf in TFS})
    base = pd.concat([n_row, base]).round(3)
    base.to_csv(OUT / "baseline_by_timeframe.csv")

    # --- breakout character -------------------------------------------------
    char = pd.DataFrame({cols[tf]: {
        "median breakout time (min after 10:30)":
            results[tf][1].loc[results[tf][1]["is_first"], "minutes_after_ib"].median(),
        "median close-through depth (R)":
            results[tf][1].loc[results[tf][1]["is_first"], "close_through"].median(),
        "median MFE after breakout (R)":
            results[tf][1].loc[results[tf][1]["is_first"], "mfe"].median(),
        "P(FVG at breakout)":
            results[tf][1].loc[results[tf][1]["is_first"], "fvg_present"].mean().round(3),
        "median CISD delay (min)":
            results[tf][1].loc[results[tf][1]["is_first"], "cisd_minutes"].median(),
    } for tf in TFS})
    char.round(3).to_csv(OUT / "breakout_character_by_timeframe.csv")

    # --- day taxonomy --------------------------------------------------------
    pat = pd.DataFrame({cols[tf]: results[tf][0]["pattern"].value_counts(normalize=True)
                        for tf in TFS}).round(3)
    pat.to_csv(OUT / "day_patterns_by_timeframe.csv")

    # --- CISD / FVG comparison -----------------------------------------------
    def cisd_row(ev):
        f = ev[ev["is_first"]]
        cs = f[f["cisd"]]
        return {
            "P(CISD | breakout)": f["cisd"].mean(),
            "P(reach IB mid | CISD)": cs["cisd_then_mid"].mean(),
            "P(reach opposite | CISD)": cs["cisd_then_opposite"].mean(),
            "median CISD delay (min)": cs["cisd_minutes"].median(),
        }

    def fvg_row(ev):
        f = ev[ev["is_first"]]
        fv = f[f["fvg_present"]]
        rv = fv[fv["fvg_revisited"]]
        return {
            "P(FVG forms)": f["fvg_present"].mean(),
            "P(revisited | FVG)": fv["fvg_revisited"].mean(),
            "P(holds -> 0.5R | revisited)": rv["fvg_held"].mean(),
            "P(reach 1R | FVG)": fv["ext_1.0"].mean(),
            "P(reach 1R | no FVG)": f[~f["fvg_present"]]["ext_1.0"].mean(),
        }

    pd.DataFrame({cols[tf]: cisd_row(results[tf][1]) for tf in TFS}).round(3) \
        .to_csv(OUT / "cisd_by_timeframe.csv")
    pd.DataFrame({cols[tf]: fvg_row(results[tf][1]) for tf in TFS}).round(3) \
        .to_csv(OUT / "fvg_by_timeframe.csv")

    # --- displacement effect at each tf (Q4 vs Q1) -----------------------------
    disp = {}
    for tf in TFS:
        f = results[tf][1][results[tf][1]["is_first"]]
        g = f.groupby("disp_q", observed=True)[["ext_1.0", "failed_before_05", "cisd",
                                                "reached_opposite_after"]].mean()
        for q in ["Q1_weak", "Q4_strong"]:
            for c in g.columns:
                disp.setdefault(f"{c} | disp {q}", {})[cols[tf]] = g.loc[q, c]
    pd.DataFrame(disp).T.round(3).to_csv(OUT / "displacement_by_timeframe.csv")

    # --- strategies --------------------------------------------------------
    strat = []
    for tf in TFS:
        s = strategy_summary(results[tf][1])
        s.insert(0, "tf", cols[tf])
        strat.append(s)
    strat = pd.concat(strat, ignore_index=True)
    strat.to_csv(OUT / "strategies_by_timeframe.csv", index=False)

    charts(results, cols)
    write_report(base, char, pat, results, strat, cols)
    print("done -> results/multitf/")


def charts(results, cols):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    xs = list(EXTENSIONS)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for tf, (days, ev) in results.items():
        f = ev[ev["is_first"]]
        ax.plot(xs, [f[f"ext_{x}"].mean() for x in xs], marker="o",
                label=f"{cols[tf]} (n={len(f)})")
    ax.set_xlabel("extension beyond IB edge (x IB range)")
    ax.set_ylabel("P(reached before EOD)")
    ax.set_title("Extension reach by signal timeframe")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "extension_by_timeframe.png")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    keys = ["failed", "failed_before_05", "cisd", "retest", "reached_opposite_after"]
    t = pd.DataFrame({cols[tf]: ev[ev["is_first"]][keys].mean()
                      for tf, (days, ev) in results.items()})
    t.plot.bar(ax=ax, rot=15)
    ax.set_ylabel("probability")
    ax.set_title("Breakout failure / reversal stats by signal timeframe")
    fig.tight_layout()
    fig.savefig(OUT / "failure_by_timeframe.png")
    plt.close("all")


def write_report(base, char, pat, results, strat, cols):
    md = lambda t: t.to_markdown()
    lines = [
        "# IB Breakouts: 1m vs 5m vs 15m signal timeframe",
        "",
        "Same IB (9:30-10:30 ET high/low), same data; only the *signal* timeframe "
        "changes. A breakout is the first close of that timeframe beyond the edge, "
        "and displacement / FVG / CISD / failure are all defined on bars of that "
        "timeframe. Outcomes (extensions, opposite-side touches) use that "
        "timeframe's highs/lows.",
        "",
        "## Baseline outcomes of the first breakout",
        "", md(base), "",
        "![extensions](extension_by_timeframe.png)",
        "![failure](failure_by_timeframe.png)",
        "",
        "## Breakout character",
        "", md(char.round(3)), "",
        "## Day taxonomy (close-based, so it shifts with timeframe)",
        "", md(pat), "",
        "## CISD", "", md(pd.read_csv(OUT / 'cisd_by_timeframe.csv', index_col=0)), "",
        "## FVG", "", md(pd.read_csv(OUT / 'fvg_by_timeframe.csv', index_col=0)), "",
        "## Displacement effect (weakest vs strongest quartile)",
        "", md(pd.read_csv(OUT / 'displacement_by_timeframe.csv', index_col=0)), "",
        "## Strategy expectancies", "", md(strat.round(3)), "",
        "*Note: higher-timeframe entries trigger later and deeper beyond the edge, "
        "so R-multiples are not directly comparable across rows — risk per trade "
        "(entry minus IB mid) grows with the timeframe.*",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
