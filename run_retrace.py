"""Retracement-entry study for 5m close-through IB breakouts.

For each first 5m close through an IB edge (per side, per session):

* Dealing range (up case): L = session low from 9:30 through the breakout bar,
  H = running post-breakout extreme (re-anchors as new highs print).
  Mirrored for down breakouts.
* Retracement depth at any bar = (H_so_far - low) / (H_so_far - L), i.e. 0% at
  the extreme, 100% at the dealing-range low.
* Question A: deepest depth reached before the FIRST new extreme after the
  breakout, and before the LAST new extreme of the day -- bucketed
  <25 / 25-50 / 50-75 / 75-100 / >100%.
* Question B: resting limit at the 25/50/75% level of the *current* dealing
  range (level trails as H extends). On fill: stop at the dealing-range low
  (100%), target back at the extreme that stood at fill time. Same-bar
  stop+target counts as stop; EOD exit at the close.

A second anchor, the breakout impulse leg low, is reported alongside.

Usage: python run_retrace.py [path-to-parquet] [tf-minutes]
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
OUT = Path(f"results/retrace_{TF}m")
OUT.mkdir(parents=True, exist_ok=True)

QS = (0.25, 0.50, 0.75)
BUCKETS = [0, 0.25, 0.50, 0.75, 1.0, np.inf]
BUCKET_LABELS = ["<25%", "25-50%", "50-75%", "75-100%", ">100%"]


@dataclass
class RetraceSetup:
    date: object
    direction: str
    bo_minutes: int
    ib_range: float
    dr_size: float            # dealing range height at breakout, points
    leg_size: float           # impulse leg height, points
    continued: bool           # any new extreme after the breakout bar
    d_first: float            # deepest depth before the first new extreme
    d_last: float             # deepest depth before the last new extreme
    d_first_leg: float        # same, anchored to the impulse leg low
    d_last_leg: float
    # limit sims: per q -> filled / depth at fill time / outcome R / hit old extreme
    q25_filled: bool = False
    q25_won: bool = False
    q25_r: float = np.nan
    q50_filled: bool = False
    q50_won: bool = False
    q50_r: float = np.nan
    q75_filled: bool = False
    q75_won: bool = False
    q75_r: float = np.nan


def _depths(h, l, b, L, sign):
    """Per-bar running-extreme depth series after breakout bar b.

    Returns (depth array for bars b+1..n-1, new_extreme bool array, H_run array).
    Depth uses the extreme standing BEFORE the bar.
    """
    n = len(h)
    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    idx = np.arange(b + 1, n)
    depth = np.empty(len(idx))
    new_ext = np.zeros(len(idx), bool)
    H = fav[b]
    H_run = np.empty(len(idx))
    for t, k in enumerate(idx):
        H_run[t] = H
        rng = (H - L) * sign
        depth[t] = (H - adv[k]) * sign / rng if rng > 0 else np.nan
        if fav[k] * sign > H * sign:
            new_ext[t] = True
            H = fav[k]
    return depth, new_ext, H_run


def _limit_sim(h, l, c, b, L, sign, q):
    """Trailing limit at q of the current dealing range. Returns (filled, won, R)."""
    n = len(h)
    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    H = fav[b]
    for k in range(b + 1, n):
        level = H - sign * q * (H - L) * sign
        if adv[k] * sign <= level * sign:
            # filled at the level (conservative: ignore better fills)
            fill, H_fill = level, H
            risk = (fill - L) * sign
            if risk <= 0:
                return True, False, np.nan
            for m in range(k, n):
                hit_stop = adv[m] * sign <= L * sign
                hit_tgt = fav[m] * sign >= H_fill * sign if m > k else False
                if hit_stop:
                    return True, False, -1.0
                if hit_tgt:
                    return True, True, (H_fill - fill) * sign / risk
            return True, False, (c[-1] - fill) * sign / risk
        if fav[k] * sign > H * sign:
            H = fav[k]
    return False, False, np.nan


def analyze(date, ib, post, sign):
    ib_high, ib_low = ib["high"].max(), ib["low"].min()
    edge = ib_high if sign > 0 else ib_low
    day = pd.concat([ib, post])
    o = day["open"].to_numpy()
    h = day["high"].to_numpy()
    l = day["low"].to_numpy()
    c = day["close"].to_numpy()
    n_ib = len(ib)

    pc = post["close"].to_numpy()
    bo = np.argmax(pc * sign > edge * sign) if ((pc * sign) > edge * sign).any() else -1
    if bo < 0:
        return None
    b = n_ib + bo

    adv_full = l if sign > 0 else h
    L_dr = (adv_full[:b + 1] * sign).min() * sign            # session extreme so far
    j = b
    while j > 0 and np.sign(c[j - 1] - o[j - 1]) == sign:
        j -= 1
    L_leg = (adv_full[j:b + 1] * sign).min() * sign          # impulse leg origin

    fav = h if sign > 0 else l
    depth, new_ext, _ = _depths(h, l, b, L_dr, sign)
    depth_leg, _, _ = _depths(h, l, b, L_leg, sign)
    continued = new_ext.any()

    def deepest_before(dep, mask_to):
        return float(np.nanmax(dep[:mask_to])) if mask_to > 0 else 0.0

    if continued:
        first_ne = int(np.argmax(new_ext))
        last_ne = int(len(new_ext) - 1 - np.argmax(new_ext[::-1]))
        d_first = deepest_before(depth, first_ne)
        d_last = deepest_before(depth, last_ne)
        d_first_leg = deepest_before(depth_leg, first_ne)
        d_last_leg = deepest_before(depth_leg, last_ne)
    else:
        d_first = d_last = float(np.nanmax(depth)) if len(depth) else np.nan
        d_first_leg = d_last_leg = float(np.nanmax(depth_leg)) if len(depth_leg) else np.nan

    s = RetraceSetup(
        date=date, direction="up" if sign > 0 else "down", bo_minutes=bo * TF,
        ib_range=ib_high - ib_low, dr_size=(fav[b] - L_dr) * sign,
        leg_size=(fav[b] - L_leg) * sign, continued=continued,
        d_first=d_first, d_last=d_last, d_first_leg=d_first_leg, d_last_leg=d_last_leg,
    )
    for q, tag in zip(QS, ["q25", "q50", "q75"]):
        filled, won, r = _limit_sim(h, l, c, b, L_dr, sign, q)
        setattr(s, f"{tag}_filled", filled)
        setattr(s, f"{tag}_won", won)
        setattr(s, f"{tag}_r", r)
    return s


def main():
    print("loading data...")
    df = resample_tf(load_continuous(DATA), TF)
    setups = []
    for date, ib, post in rth_days(df, TF):
        for sign in (+1, -1):
            s = analyze(date, ib, post, sign)
            if s is not None:
                setups.append(s)
    su = pd.DataFrame([asdict(s) for s in setups])
    su.to_csv(OUT / "setups.csv", index=False)
    cont = su[su["continued"]]
    print(f"{len(su)} close-through breakouts, {len(cont)} ({len(cont)/len(su):.0%}) "
          f"made a new extreme after the breakout bar")

    # --- A: where does it retrace to before continuing ---------------------
    def bucket(s):
        return pd.cut(s, BUCKETS, labels=BUCKET_LABELS, right=False).value_counts(
            normalize=True).reindex(BUCKET_LABELS)

    dist = pd.DataFrame({
        "before first new extreme (DR)": bucket(cont["d_first"]),
        "before last new extreme (DR)": bucket(cont["d_last"]),
        "before first new extreme (leg)": bucket(cont["d_first_leg"]),
        "before last new extreme (leg)": bucket(cont["d_last_leg"]),
    }).round(3)
    dist.to_csv(OUT / "retrace_distribution.csv")

    med = pd.DataFrame({
        "median": [cont["d_first"].median(), cont["d_last"].median(),
                   cont["d_first_leg"].median(), cont["d_last_leg"].median()],
        "p75": [cont["d_first"].quantile(.75), cont["d_last"].quantile(.75),
                cont["d_first_leg"].quantile(.75), cont["d_last_leg"].quantile(.75)],
    }, index=dist.columns).round(3)
    med.to_csv(OUT / "retrace_medians.csv")

    # --- B: trailing limit at 25/50/75% ------------------------------------
    rows = []
    for q, tag in zip(QS, ["q25", "q50", "q75"]):
        f = su[su[f"{tag}_filled"]]
        r = f[f"{tag}_r"].dropna()
        rows.append({
            "level": f"{int(q*100)}%", "P(fill)": su[f"{tag}_filled"].mean(),
            "n_filled": len(f),
            "P(back to extreme | fill)": f[f"{tag}_won"].mean(),
            "P(stop 100% | fill)": (r == -1.0).mean(),
            "avg_R": r.mean(), "win_rate": (r > 0).mean(),
            "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
            "reward_risk_at_fill": q / (1 - q),
        })
    strat = pd.DataFrame(rows).round(3)
    strat.to_csv(OUT / "limit_strategy.csv", index=False)

    # conditioning of the 50% fill
    su["time_b"] = pd.cut(su["bo_minutes"], [-1, 30, 90, 1000],
                          labels=["10:30-11:00", "11:00-12:00", "after 12:00"])
    conds = {}
    for cond in ["direction", "time_b"]:
        g = su.groupby(cond, observed=True)
        conds[cond] = pd.DataFrame({
            "n": g.size(),
            "P(fill 50%)": g["q50_filled"].mean(),
            "P(win | fill 50%)": g.apply(lambda d: d.loc[d["q50_filled"], "q50_won"].mean()),
            "avg_R (50%)": g.apply(lambda d: d.loc[d["q50_filled"], "q50_r"].mean()),
        }).round(3)
    pd.concat(conds).to_csv(OUT / "conditioned.csv")

    charts(su, cont, dist, strat)
    report(su, cont, dist, med, strat, conds)
    print(f"done -> {OUT}/")


def charts(su, cont, dist, strat):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})

    fig, ax = plt.subplots(figsize=(8, 4.5))
    dist[["before first new extreme (DR)", "before last new extreme (DR)"]].plot.bar(ax=ax, rot=0)
    ax.set_ylabel("share of continuing breakouts")
    ax.set_xlabel("deepest retracement of the dealing range before continuation")
    ax.set_title(f"{TF}m close-through breakouts: retracement depth before new extremes")
    fig.tight_layout()
    fig.savefig(OUT / "retrace_distribution.png")

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(cont["d_last"].clip(upper=1.5), bins=45)
    for q in QS:
        ax.axvline(q, color="r", ls="--", lw=1)
    ax.set_xlabel("deepest DR retracement before the day's last new extreme")
    ax.set_ylabel("breakouts")
    ax.set_title("Retracement depth distribution (red: 25/50/75%)")
    fig.tight_layout()
    fig.savefig(OUT / "retrace_hist.png")
    plt.close("all")


def report(su, cont, dist, med, strat, conds):
    md = lambda t: t.to_markdown()
    lines = [
        f"# {TF}m Close-Through Breakouts: retracement entries on the dealing range",
        "",
        f"Setups: {len(su)} first close-through IB breakouts (per side per session); "
        f"{len(cont)} ({len(cont)/len(su):.0%}) printed a new extreme after the "
        "breakout bar (the continuation population the depth tables condition on).",
        "",
        "**Dealing range (DR):** session extreme-so-far at the breakout (low for "
        "up-breakouts) to the running post-breakout extreme; depth 0% = at the "
        "extreme, 100% = full negation. The *leg* anchor instead uses the low of "
        "the consecutive-close impulse run into the breakout. Depth is measured "
        "against the extreme standing at the time of each bar, so the levels trail "
        "as the move extends.",
        "",
        "## A. Deepest retracement before price resumed",
        "", md(dist), "",
        "Medians / 75th percentiles:", "", md(med), "",
        "![dist](retrace_distribution.png)",
        "![hist](retrace_hist.png)",
        "",
        "## B. Resting limit at the 25/50/75% DR level",
        "",
        "Limit trails the current DR; stop at the 100% level (DR low), target = "
        "the extreme standing at fill time, EOD exit at the close, same-bar "
        "stop+target counted as stop.",
        "", md(strat.set_index("level")), "",
        "## Conditioned (50% level)", "",
    ]
    for name, t in conds.items():
        lines += [f"### by {name}", "", md(t), ""]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
