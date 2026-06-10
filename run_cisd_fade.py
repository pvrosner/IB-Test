"""CISD fade study on 5m bars: fade IB-edge raids targeting manipulation-leg projections.

Setup (bullish raid faded short; mirrored for downside raids):
  1. After 10:30 ET, price trades above the IB high (wick or close) -- the raid.
  2. The *manipulation leg* is the run of consecutive up-close 5m candles ending
     at (or nearest before) the bar that printed the post-raid session extreme.
     If a new extreme prints before the fade triggers, the leg re-anchors.
  3. CISD: the first 5m close below the OPEN of the first candle of that run.
     Entry at that close. Stop at the manipulation high (wick).
  4. Targets: body-based projections of the leg. leg_top = highest body in the
     run, leg_bottom = origin open. Levels = leg_top - x * (leg_top - leg_bottom)
     for x in 0.5, 1, 1.5, 2, 2.5, 3, 4 -- i.e. 1.0 is a full body-retrace of the
     run-up, 2.0 a symmetric extension below it.

One setup per side per day (the first trigger). Same-bar stop+target counts as
stop. EOD flat exit at the last bar's close.

Usage: python run_cisd_fade.py [path-to-parquet] [tf-minutes]
"""

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous, resample_tf, rth_days

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
TF = int(sys.argv[2]) if len(sys.argv) > 2 else 5
OUT = Path(f"results/cisd_fade_{TF}m")
OUT.mkdir(parents=True, exist_ok=True)

PROJECTIONS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


@dataclass
class FadeSetup:
    date: object
    side: str                 # 'short' fades an upside raid, 'long' a downside raid
    raid_type: str            # 'sweep' (wick only) or 'close_through'
    raid_minutes: int         # raid start, minutes after 10:30
    entry_minutes: int        # CISD close, minutes after 10:30
    raid_depth_r: float       # extreme beyond IB edge, in IB-range units
    leg_bars: int
    leg_size: float           # body top - body bottom, points
    leg_size_r: float         # in IB-range units
    entry: float
    stop: float
    risk_pts: float
    entry_inside_ib: bool     # CISD close back inside the IB range
    entry_proj: float         # how far down the projection scale entry already sits
    stopped: bool = False
    max_proj: float = 0.0     # deepest projection reached before stop/EOD
    minutes_to_1x: float = np.nan


def detect_fades(date, ib, post, sign):
    """sign=+1: upside raid faded short. sign=-1: downside raid faded long."""
    ib_high, ib_low = ib["high"].max(), ib["low"].min()
    rng = ib_high - ib_low
    edge = ib_high if sign > 0 else ib_low
    o = post["open"].to_numpy()
    h = post["high"].to_numpy()
    l = post["low"].to_numpy()
    c = post["close"].to_numpy()
    n = len(c)
    fav = h if sign > 0 else l  # raid side extreme series

    raid = np.argmax(fav * sign > edge * sign) if ((fav * sign) > edge * sign).any() else -1
    if raid < 0:
        return None

    # walk forward maintaining the extreme, its manipulation leg and CISD level
    ext_bar = raid
    closed_through = False
    for i in range(raid, n):
        if fav[i] * sign >= fav[ext_bar] * sign:
            ext_bar = i
        if c[i] * sign > edge * sign:
            closed_through = True
        # leg: consecutive sign-direction closes ending at/nearest before ext_bar
        a = ext_bar
        while a > 0 and np.sign(c[a] - o[a]) != sign:
            a -= 1
        j = a
        while j > 0 and np.sign(c[j - 1] - o[j - 1]) == sign:
            j -= 1
        cisd_level = o[j]
        if i > ext_bar and c[i] * sign < cisd_level * sign:
            # CISD triggered at bar i
            bod = np.concatenate([o[j:a + 1], c[j:a + 1]])
            leg_top = (bod * sign).max() * sign
            leg_bottom = o[j]
            leg = (leg_top - leg_bottom) * sign
            if leg <= 0:
                return None
            stop = fav[ext_bar]
            entry = c[i]
            risk = (stop - entry) * sign
            if risk <= 0:
                return None
            setup = FadeSetup(
                date=date, side="short" if sign > 0 else "long",
                raid_type="close_through" if closed_through else "sweep",
                raid_minutes=raid * TF, entry_minutes=i * TF,
                raid_depth_r=(fav[ext_bar] - edge) * sign / rng,
                leg_bars=a - j + 1, leg_size=leg, leg_size_r=leg / rng,
                entry=entry, stop=stop, risk_pts=risk,
                entry_inside_ib=(c[i] < ib_high and c[i] > ib_low),
                entry_proj=(leg_top - entry) * sign / leg,
            )
            # walk the rest of the day
            adverse = h if sign > 0 else l
            good = l if sign > 0 else h
            for k in range(i + 1, n):
                if adverse[k] * sign >= stop * sign:
                    setup.stopped = True
                    break
                reach = (leg_top - good[k]) * sign / leg
                if reach > setup.max_proj:
                    setup.max_proj = reach
                if np.isnan(setup.minutes_to_1x) and reach >= 1.0:
                    setup.minutes_to_1x = (k - i) * TF
            return setup
    return None


def main():
    print("loading data...")
    df = resample_tf(load_continuous(DATA), TF)
    setups = []
    for date, ib, post in rth_days(df, TF):
        for sign in (+1, -1):
            s = detect_fades(date, ib, post, sign)
            if s is not None:
                setups.append(s)

    su = pd.DataFrame([asdict(s) for s in setups])
    su.to_csv(OUT / "setups.csv", index=False)
    print(f"{len(su)} fade setups on {TF}m bars "
          f"({(su['side'] == 'short').sum()} short / {(su['side'] == 'long').sum()} long)")

    # --- probability of reaching each projection --------------------------
    def reach_table(d):
        rows = {}
        for x in PROJECTIONS:
            reached = d["max_proj"] >= x
            rows[f"{x}x"] = {
                "P(reach before stop/EOD)": reached.mean(),
                "P(reach | not stopped first)": np.nan,  # filled below
                "n": len(d),
            }
        return rows

    tables = {}
    for name, d in [("all", su), ("short", su[su["side"] == "short"]),
                    ("long", su[su["side"] == "long"]),
                    ("sweep", su[su["raid_type"] == "sweep"]),
                    ("close_through", su[su["raid_type"] == "close_through"])]:
        t = pd.DataFrame(reach_table(d)).T[["P(reach before stop/EOD)", "n"]]
        tables[name] = t

    reach = pd.concat({k: v["P(reach before stop/EOD)"] for k, v in tables.items()}, axis=1)
    reach.loc["n"] = {k: int(v["n"].iloc[0]) for k, v in tables.items()}
    reach.round(3).to_csv(OUT / "projection_reach.csv")

    # --- strategy: target each projection, stop at manipulation high ------
    rows = []
    for x in PROJECTIONS:
        for name, d in [("all", su), ("sweep only", su[su["raid_type"] == "sweep"]),
                        ("close-through only", su[su["raid_type"] == "close_through"])]:
            d = d[d["entry_proj"] < x]  # target must sit beyond the entry price
            if len(d) < 30:
                continue
            tgt_reached = d["max_proj"] >= x
            r = pd.Series(np.where(tgt_reached,
                                   (x - d["entry_proj"]) * d["leg_size"] / d["risk_pts"],
                                   np.nan), index=d.index)
            r[d["stopped"] & ~tgt_reached] = -1.0
            # EOD exits (neither stop nor target): conservative 0R
            r = r.fillna(0.0)
            rows.append({
                "target": f"{x}x", "filter": name, "n": len(d),
                "P(target)": tgt_reached.mean(), "P(stop)": (d["stopped"] & ~tgt_reached).mean(),
                "P(EOD exit)": 1 - tgt_reached.mean() - (d["stopped"] & ~tgt_reached).mean(),
                "avg_R": r.mean(), "win_rate": (r > 0).mean(),
                "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
            })
    strat = pd.DataFrame(rows).round(3)
    strat.to_csv(OUT / "strategy_by_target.csv", index=False)

    # --- conditioning ------------------------------------------------------
    su["raid_depth_b"] = pd.qcut(su["raid_depth_r"], 3, labels=["shallow", "medium", "deep"])
    su["leg_size_b"] = pd.qcut(su["leg_size_r"], 3, labels=["small leg", "mid leg", "big leg"])
    su["time_b"] = pd.cut(su["entry_minutes"], [-1, 60, 150, 1000],
                          labels=["before 11:30", "11:30-13:00", "after 13:00"])
    conds = {}
    for cond in ["raid_depth_b", "leg_size_b", "time_b", "entry_inside_ib", "raid_type", "side"]:
        g = su.groupby(cond, observed=True)
        t = pd.DataFrame({
            "n": g.size(),
            "P(1x)": g["max_proj"].agg(lambda s: (s >= 1).mean()),
            "P(2x)": g["max_proj"].agg(lambda s: (s >= 2).mean()),
            "P(stopped)": g["stopped"].mean(),
            "median max_proj": g["max_proj"].median(),
        }).round(3)
        conds[cond] = t
    pd.concat(conds).to_csv(OUT / "conditioned.csv")

    charts(su)
    report(su, reach, strat, conds)
    print(f"done -> {OUT}/")


def charts(su):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    xs = list(PROJECTIONS)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for name, d in [("all", su), ("sweep", su[su["raid_type"] == "sweep"]),
                    ("close_through", su[su["raid_type"] == "close_through"])]:
        ax.plot(xs, [(d["max_proj"] >= x).mean() for x in xs], marker="o",
                label=f"{name} (n={len(d)})")
    ax.set_xlabel("projection of manipulation-leg bodies (x leg)")
    ax.set_ylabel("P(reached before stop/EOD)")
    ax.set_title(f"{TF}m CISD fade: how far does the reversal deliver?")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "projection_reach.png")

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(su["max_proj"].clip(upper=5), bins=50)
    for x in (1, 2):
        ax.axvline(x, color="r", ls="--", lw=1)
    ax.set_xlabel("deepest projection reached (x leg, clipped at 5)")
    ax.set_ylabel("setups")
    ax.set_title(f"{TF}m CISD fade: distribution of delivery depth")
    fig.tight_layout()
    fig.savefig(OUT / "max_proj_hist.png")
    plt.close("all")


def report(su, reach, strat, conds):
    md = lambda t: t.to_markdown()
    lines = [
        f"# {TF}m CISD Fade Study: IB-edge raids, manipulation-leg projections",
        "",
        f"Setups: {len(su)} ({(su['side'] == 'short').sum()} fading upside raids, "
        f"{(su['side'] == 'long').sum()} fading downside raids), one per side per "
        "session, first CISD trigger only.",
        "",
        "**Definitions.** Raid = post-10:30 trade beyond an IB edge. Manipulation "
        "leg = consecutive same-direction 5m closes into the post-raid extreme "
        "(re-anchored if a new extreme prints before the trigger). CISD = first "
        "5m close through the leg origin's open; entry at that close, stop at the "
        "raid extreme (wick). Projections measured on leg BODIES: leg top = "
        "highest body of the run, leg bottom = origin open; level x = leg_top - "
        "x * leg_body_size. 1.0x = full body-retrace of the run-up; 2.0x = "
        "symmetric extension below it. Same-bar stop+target counts as stop; "
        "EOD exits counted as 0R in the strategy table (conservative).",
        "",
        f"Median risk (entry to stop): {su['risk_pts'].median():.1f} pts; "
        f"median leg size {su['leg_size'].median():.1f} pts; median entry already "
        f"{su['entry_proj'].median():.2f}x down the projection scale at trigger.",
        "",
        "## Probability of delivering to each projection",
        "", md(reach), "",
        "![reach](projection_reach.png)",
        "![hist](max_proj_hist.png)",
        "",
        "## Fixed-target strategy (stop at raid extreme)",
        "", md(strat.set_index(["target", "filter"])), "",
        "## Conditioned",
        "",
    ]
    for name, t in conds.items():
        lines += [f"### by {name}", "", md(t), ""]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
