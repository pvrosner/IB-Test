"""Conditional probability tables and strategy expectancy summaries."""

import numpy as np
import pandas as pd

OUTCOMES = ["ext_0.25", "ext_0.5", "ext_1.0", "ext_1.5", "ext_2.0",
            "failed", "failed_before_05", "cisd", "reached_opposite_after",
            "retest", "resumed_after_retest", "eod_beyond_edge"]


def wilson_halfwidth(p, n, z=1.96):
    """Half-width of the Wilson score interval (for ± display)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / (1 + z**2 / n), np.nan)


def conditional_table(df: pd.DataFrame, cond: str, outcomes=OUTCOMES) -> pd.DataFrame:
    """P(outcome | cond bucket) with sample sizes."""
    present = [o for o in outcomes if o in df.columns]
    g = df.groupby(cond, observed=True)
    out = g[present].mean()
    out.insert(0, "n", g.size())
    return out.round(3)


def add_buckets(ev: pd.DataFrame) -> pd.DataFrame:
    """Attach the condition buckets used by the probability engine."""
    ev = ev.copy()
    ev["disp_q"] = pd.qcut(ev["disp_ratio"], 4, labels=["Q1_weak", "Q2", "Q3", "Q4_strong"])
    ev["close_through_b"] = pd.cut(ev["close_through"], [0, 0.05, 0.15, 0.30, np.inf],
                                   labels=["<0.05R", "0.05-0.15R", "0.15-0.30R", ">0.30R"])
    ev["time_b"] = pd.cut(ev["minutes_after_ib"], [-1, 30, 90, 180, 1000],
                          labels=["10:30-11:00", "11:00-12:00", "12:00-13:30", "after 13:30"])
    ev["ib_width_q"] = pd.qcut(ev["ib_range_pctile"], 5,
                               labels=["narrowest", "narrow", "mid", "wide", "widest"])
    ev["dow_name"] = ev["dow"].map({0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"})
    return ev


def strategy_summary(ev: pd.DataFrame) -> pd.DataFrame:
    """Expectancy of the three simulated tactics, overall and filtered."""
    rows = []

    def add(name, r):
        r = r.dropna()
        if len(r) < 20:
            return
        rows.append({
            "strategy": name, "n": len(r), "win_rate": (r > 0).mean(),
            "avg_R": r.mean(), "median_R": r.median(), "total_R": r.sum(),
            "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
        })

    first = ev[ev["is_first"]]
    add("S1 breakout close (stop mid, tgt 1R ext)", first["s1_breakout_r"])
    add("S1 + displacement Q4", first[first["disp_q"] == "Q4_strong"]["s1_breakout_r"])
    add("S1 + FVG present", first[first["fvg_present"]]["s1_breakout_r"])
    add("S1 + FVG + disp Q3/Q4", first[first["fvg_present"]
        & first["disp_q"].isin(["Q3", "Q4_strong"])]["s1_breakout_r"])
    add("S1 early (10:30-12:00)", first[first["minutes_after_ib"] <= 90]["s1_breakout_r"])
    add("S1 + narrow IB (bottom 40%)", first[first["ib_width_q"].isin(["narrowest", "narrow"])]["s1_breakout_r"])
    add("S1 + wide IB (top 40%)", first[first["ib_width_q"].isin(["wide", "widest"])]["s1_breakout_r"])

    add("S2 retest limit at edge (tgt 0.5R ext)", first["s2_retest_r05"])
    add("S2 retest limit at edge (tgt 1R ext)", first["s2_retest_r10"])
    add("S2 (0.5R) + FVG present", first[first["fvg_present"]]["s2_retest_r05"])
    add("S2 (0.5R) + displacement Q3/Q4",
        first[first["disp_q"].isin(["Q3", "Q4_strong"])]["s2_retest_r05"])

    add("S3 CISD fade to opposite edge", first["s3_cisd_fade_r"])
    add("S3 + weak breakout (disp Q1/Q2)",
        first[first["disp_q"].isin(["Q1_weak", "Q2"])]["s3_cisd_fade_r"])
    add("S3 + early CISD (<60m after breakout)",
        first[first["cisd_minutes"] <= 60]["s3_cisd_fade_r"])

    return pd.DataFrame(rows).round(3)
