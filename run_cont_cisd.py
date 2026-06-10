"""Continuation-CISD entries in the 25-50% dealing-range zone (5m).

Setup (bull case; mirrored for down breakouts):
  1. First 5m close through the IB high after 10:30 ET (close-through breakout).
  2. Dealing range: L = session low through the breakout bar (fixed),
     H = trailing post-breakout extreme. Depth = (H - low) / (H - L).
  3. Price retraces into the 25-50% zone. If depth exceeds 50% before a trigger,
     the episode is invalid until a new extreme resets it.
  4. The retracement (CISD) leg = run of consecutive down-close 5m candles
     ending at (or nearest before) the episode low; it re-anchors while new
     episode lows print. Bullish CISD = first 5m close above the open of the
     first candle of that run, while deepest episode depth is within 25-50%
     and the close is still below H.
  5. Entry at the CISD close. STOP AT THE LOW OF THE CISD LEG (the episode low).
     Targets: the standing extreme H, H + 0.25*DR, H + 0.5*DR (DR = H - L at
     trigger). Same-bar stop+target counts as stop; EOD exit at the close.

First trigger per breakout, one breakout per side per session.

Usage: python run_cont_cisd.py [path-to-parquet] [tf-minutes]
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
OUT = Path(f"results/cont_cisd_{TF}m")
OUT.mkdir(parents=True, exist_ok=True)

ZONE = (0.25, 0.50)
TARGETS = {"old_extreme": 0.0, "ext_0.25dr": 0.25, "ext_0.5dr": 0.5}


@dataclass
class ContSetup:
    date: object
    direction: str
    bo_minutes: int           # breakout, minutes after 10:30
    trigger_minutes: int      # CISD close, minutes after 10:30
    depth_at_trigger: float   # deepest episode depth when CISD fired
    dr_size: float            # H - L at trigger, points
    leg_bars: int             # bars in the retracement run
    entry: float
    stop: float               # CISD-leg low (episode low)
    risk_pts: float
    risk_dr: float            # risk as fraction of the dealing range
    dist_to_extreme_r: float  # (H - entry) / risk: R available to the old extreme
    mfe_r: float = 0.0
    stopped_first: bool = False
    hit_old_extreme: bool = False
    hit_ext_025: bool = False
    hit_ext_05: bool = False
    r_old_extreme: float = np.nan
    r_ext_025: float = np.nan
    r_ext_05: float = np.nan


def find_setup(date, ib, post, sign):
    ib_high, ib_low = ib["high"].max(), ib["low"].min()
    edge = ib_high if sign > 0 else ib_low
    day = pd.concat([ib, post])
    o = day["open"].to_numpy()
    h = day["high"].to_numpy()
    l = day["low"].to_numpy()
    c = day["close"].to_numpy()
    n_ib, n = len(ib), len(day)

    pc = post["close"].to_numpy()
    bo = np.argmax(pc * sign > edge * sign) if ((pc * sign) > edge * sign).any() else -1
    if bo < 0:
        return None
    b = n_ib + bo

    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    L = (adv[:b + 1] * sign).min() * sign

    H = fav[b]
    lo = None       # episode extreme (deepest adverse since last new extreme)
    lo_bar = -1
    valid = True

    for k in range(b + 1, n):
        if fav[k] * sign > H * sign:
            H = fav[k]
            lo, lo_bar, valid = None, -1, True
            continue
        if lo is None or adv[k] * sign < lo * sign:
            lo, lo_bar = adv[k], k
        dr = (H - L) * sign
        depth = (H - lo) * sign / dr
        if depth > ZONE[1]:
            valid = False
        if not valid or depth < ZONE[0]:
            continue
        # retracement leg: consecutive counter-direction closes ending at/nearest
        # before the episode low bar
        a = lo_bar
        while a > b and np.sign(c[a] - o[a]) != -sign:
            a -= 1
        j = a
        while j > b and np.sign(c[j - 1] - o[j - 1]) == -sign:
            j -= 1
        cisd_level = o[j]
        if k > lo_bar or (k == lo_bar and np.sign(c[k] - o[k]) == sign):
            if c[k] * sign > cisd_level * sign and c[k] * sign < H * sign:
                return _simulate(date, sign, bo, k - n_ib, depth, H, L, lo,
                                 a - j + 1, o, h, l, c, k, n)
    return None


def _simulate(date, sign, bo, trig, depth, H, L, stop, leg_bars, o, h, l, c, k, n):
    entry = c[k]
    risk = (entry - stop) * sign
    if risk <= 0:
        return None
    dr = (H - L) * sign
    tgt_px = {name: H + sign * x * dr for name, x in TARGETS.items()}
    s = ContSetup(
        date=date, direction="up" if sign > 0 else "down",
        bo_minutes=bo * TF, trigger_minutes=trig * TF,
        depth_at_trigger=depth, dr_size=dr, leg_bars=leg_bars,
        entry=entry, stop=stop, risk_pts=risk, risk_dr=risk / dr,
        dist_to_extreme_r=(H - entry) * sign / risk,
    )
    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    hit = {name: False for name in TARGETS}
    stopped_at = n
    for m in range(k + 1, n):
        if not s.stopped_first and adv[m] * sign <= stop * sign:
            s.stopped_first = True
            stopped_at = m
            break
        s.mfe_r = max(s.mfe_r, (fav[m] - entry) * sign / risk)
        for name, px in tgt_px.items():
            if fav[m] * sign >= px * sign:
                hit[name] = True
    eod_r = (c[min(stopped_at, n) - 1] - entry) * sign / risk if stopped_at == n else np.nan
    for name, flag in [("old_extreme", "hit_old_extreme"),
                       ("ext_0.25dr", "hit_ext_025"), ("ext_0.5dr", "hit_ext_05")]:
        setattr(s, flag, hit[name])
        r = -1.0 if (s.stopped_first and not hit[name]) else (
            (tgt_px[name] - entry) * sign / risk if hit[name] else eod_r)
        setattr(s, {"old_extreme": "r_old_extreme", "ext_0.25dr": "r_ext_025",
                    "ext_0.5dr": "r_ext_05"}[name], r)
    return s


def main():
    print("loading data...")
    df = resample_tf(load_continuous(DATA), TF)
    setups, n_bo = [], 0
    for date, ib, post in rth_days(df, TF):
        for sign in (+1, -1):
            ibh, ibl = ib["high"].max(), ib["low"].min()
            e = ibh if sign > 0 else ibl
            if ((post["close"].to_numpy() * sign) > e * sign).any():
                n_bo += 1
            s = find_setup(date, ib, post, sign)
            if s is not None:
                setups.append(s)
    su = pd.DataFrame([asdict(s) for s in setups])
    su.to_csv(OUT / "setups.csv", index=False)
    print(f"{len(su)} setups from {n_bo} close-through breakouts "
          f"({len(su)/n_bo:.0%} develop the zone-CISD sequence)")

    summary = pd.Series({
        "setups": len(su),
        "P(setup | close-through breakout)": len(su) / n_bo,
        "median risk (pts)": su["risk_pts"].median(),
        "median risk (frac of DR)": su["risk_dr"].median(),
        "median R available to old extreme": su["dist_to_extreme_r"].median(),
        "median trigger time (min after 10:30)": su["trigger_minutes"].median(),
        "median depth at trigger": su["depth_at_trigger"].median(),
        "P(stopped before any target)": (su["stopped_first"] & ~su["hit_old_extreme"]).mean(),
        "median MFE (R)": su["mfe_r"].median(),
    }).round(3)
    summary.to_csv(OUT / "summary.csv")

    rows = []
    for name, flag, rcol in [("old extreme", "hit_old_extreme", "r_old_extreme"),
                             ("extreme +0.25 DR", "hit_ext_025", "r_ext_025"),
                             ("extreme +0.5 DR", "hit_ext_05", "r_ext_05")]:
        r = su[rcol].dropna()
        rows.append({
            "target": name, "n": len(r), "P(target)": su[flag].mean(),
            "P(stop first)": (su["stopped_first"] & ~su[flag]).mean(),
            "win_rate": (r > 0).mean(), "avg_R": r.mean(), "median_R": r.median(),
            "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
        })
    strat = pd.DataFrame(rows).round(3)
    strat.to_csv(OUT / "strategy.csv", index=False)

    su["time_b"] = pd.cut(su["bo_minutes"], [-1, 30, 90, 1000],
                          labels=["10:30-11:00", "11:00-12:00", "after 12:00"])
    su["depth_b"] = pd.cut(su["depth_at_trigger"], [0.25, 0.375, 0.50],
                           labels=["25-37.5%", "37.5-50%"])
    conds = {}
    for cond in ["direction", "time_b", "depth_b"]:
        g = su.groupby(cond, observed=True)
        conds[cond] = pd.DataFrame({
            "n": g.size(),
            "P(old extreme)": g["hit_old_extreme"].mean(),
            "P(+0.5 DR)": g["hit_ext_05"].mean(),
            "P(stop first)": g.apply(lambda d: (d["stopped_first"] & ~d["hit_old_extreme"]).mean()),
            "avg_R (old extreme)": g["r_old_extreme"].mean(),
            "avg_R (+0.5 DR)": g["r_ext_05"].mean(),
        }).round(3)
    pd.concat(conds).to_csv(OUT / "conditioned.csv")

    charts(su)
    report(su, n_bo, summary, strat, conds)
    print(f"done -> {OUT}/")


def charts(su):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(su["mfe_r"].clip(upper=10), bins=50)
    ax.axvline(su["dist_to_extreme_r"].median(), color="r", ls="--", lw=1,
               label=f"median R to old extreme ({su['dist_to_extreme_r'].median():.1f})")
    ax.set_xlabel("max favorable excursion (R, clipped at 10)")
    ax.set_ylabel("setups")
    ax.set_title(f"{TF}m zone-CISD continuation: MFE in risk units")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "mfe_hist.png")

    fig, ax = plt.subplots(figsize=(7, 4.5))
    r = su["r_old_extreme"].dropna().clip(-1.5, 8)
    ax.hist(r, bins=50)
    ax.axvline(0, color="k", lw=1)
    ax.set_xlabel("trade R (target = old extreme, clipped)")
    ax.set_ylabel("setups")
    ax.set_title("R distribution, stop at CISD-leg low, target = standing extreme")
    fig.tight_layout()
    fig.savefig(OUT / "r_hist.png")
    plt.close("all")


def report(su, n_bo, summary, strat, conds):
    md = lambda t: t.to_markdown()
    lines = [
        f"# {TF}m Continuation CISD in the 25-50% Dealing-Range Zone",
        "",
        "Close-through IB breakout -> retracement whose deepest depth sits in "
        "25-50% of the dealing range (session extreme-so-far to trailing "
        "post-breakout extreme; episodes that exceed 50% are invalidated until a "
        "new extreme resets them) -> first 5m close back through the open of the "
        "retracement leg (consecutive counter-direction closes into the episode "
        "low, re-anchored on new lows), closing still below the standing extreme. "
        "Entry at that close. **Stop at the low of the CISD leg.** Same-bar "
        "stop+target counts as stop; EOD exit at the close.",
        "",
        "## Summary", "", md(summary.to_frame("value")), "",
        "## Fixed targets", "", md(strat.set_index("target")), "",
        "![mfe](mfe_hist.png)",
        "![r](r_hist.png)",
        "",
        "## Conditioned", "",
    ]
    for name, t in conds.items():
        lines += [f"### by {name}", "", md(t), ""]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
