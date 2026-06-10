"""Grid study: continuation CISDs at any pullback depth, two stop placements.

Extends run_cont_cisd.py:

* Zone widened: a continuation CISD is valid at ANY pullback depth as long as
  the retracement never violates the dealing-range origin L (the session
  extreme-so-far at the breakout -- "the beginning extreme of the initial
  move"). If L is violated, the breakout's structure is dead and scanning
  stops for that side/day.
* ALL triggers are collected, not just the first: one per episode low -- the
  scanner re-arms when the episode prints a deeper low (leg re-anchors) or
  when a new extreme starts a new episode.
* Each trigger is simulated with TWO stops:
    - leg low: the episode low (as before)
    - trigger candle: the low of the CISD trigger bar itself
  Targets: standing extreme, extreme +0.25 DR, extreme +0.5 DR. Same-bar
  stop+target counts as stop; EOD exit at the close.

Usage: python run_cont_cisd_grid.py [path-to-parquet] [tf-minutes]
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ib_engine.data import load_continuous, resample_tf, rth_days

DATA = sys.argv[1] if len(sys.argv) > 1 else "data/NQ_FUT_ohlcv_1m_2021-04-01_2026-04-28.parquet"
TF = int(sys.argv[2]) if len(sys.argv) > 2 else 5
OUT = Path(f"results/cont_cisd_grid_{TF}m")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS = {"old_extreme": 0.0, "ext_025": 0.25, "ext_05": 0.5}
DEPTH_BUCKETS = [0, 0.25, 0.50, 0.75, 1.0]
DEPTH_LABELS = ["<25%", "25-50%", "50-75%", "75-100%"]


def _sim(h, l, c, k, n, sign, entry, stop, tgt_px):
    risk = (entry - stop) * sign
    if risk <= 0:
        return None
    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    out = {"risk_pts": risk, "stopped_first": False, "mfe_r": 0.0}
    hit = {name: False for name in tgt_px}
    stopped_at = n
    for m in range(k + 1, n):
        if adv[m] * sign <= stop * sign:
            out["stopped_first"] = True
            stopped_at = m
            break
        out["mfe_r"] = max(out["mfe_r"], (fav[m] - entry) * sign / risk)
        for name, px in tgt_px.items():
            if fav[m] * sign >= px * sign:
                hit[name] = True
    eod_r = (c[n - 1] - entry) * sign / risk if stopped_at == n else np.nan
    for name in tgt_px:
        out[f"hit_{name}"] = hit[name]
        out[f"r_{name}"] = -1.0 if (out["stopped_first"] and not hit[name]) else (
            (tgt_px[name] - entry) * sign / out["risk_pts"] if hit[name] else eod_r)
    return out


def find_triggers(date, ib, post, sign):
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
        return []
    b = n_ib + bo

    fav = h if sign > 0 else l
    adv = l if sign > 0 else h
    L = (adv[:b + 1] * sign).min() * sign

    rows = []
    H = fav[b]
    lo, lo_bar = None, -1
    lo_at_last_trigger = None
    rank = 0

    for k in range(b + 1, n):
        if adv[k] * sign <= L * sign:
            break  # origin of the move violated: structure dead
        if fav[k] * sign > H * sign:
            H = fav[k]
            lo, lo_bar, lo_at_last_trigger = None, -1, None
            continue
        if lo is None or adv[k] * sign < lo * sign:
            lo, lo_bar = adv[k], k
        if lo is None:
            continue
        # re-arm only if this episode hasn't triggered, or the low deepened since
        if lo_at_last_trigger is not None and not (lo * sign < lo_at_last_trigger * sign):
            continue
        dr = (H - L) * sign
        depth = (H - lo) * sign / dr
        # CISD leg into the episode low
        a = lo_bar
        while a > b and np.sign(c[a] - o[a]) != -sign:
            a -= 1
        j = a
        while j > b and np.sign(c[j - 1] - o[j - 1]) == -sign:
            j -= 1
        cisd_level = o[j]
        if (k > lo_bar or (k == lo_bar and np.sign(c[k] - o[k]) == sign)) \
                and c[k] * sign > cisd_level * sign and c[k] * sign < H * sign:
            entry = c[k]
            tgt_px = {name: H + sign * x * dr for name, x in TARGETS.items()}
            trig_stop = l[k] if sign > 0 else h[k]
            row = {
                "date": date, "direction": "up" if sign > 0 else "down",
                "bo_minutes": bo * TF, "trigger_minutes": (k - n_ib) * TF,
                "trigger_rank": rank + 1, "depth_at_trigger": depth,
                "dr_size": dr, "entry": entry,
                "dist_to_extreme_pts": (H - entry) * sign,
            }
            ok = False
            for stop_name, stop_px in [("leg", lo), ("trig", trig_stop)]:
                sim = _sim(h, l, c, k, n, sign, entry, stop_px, tgt_px)
                if sim is not None:
                    ok = True
                    for key, val in sim.items():
                        row[f"{stop_name}_{key}"] = val
            if ok:
                rows.append(row)
                rank += 1
                lo_at_last_trigger = lo
    return rows


def main():
    print("loading data...")
    df = resample_tf(load_continuous(DATA), TF)
    rows = []
    for date, ib, post in rth_days(df, TF):
        for sign in (+1, -1):
            rows.extend(find_triggers(date, ib, post, sign))
    su = pd.DataFrame(rows)
    su["depth_b"] = pd.cut(su["depth_at_trigger"], DEPTH_BUCKETS, labels=DEPTH_LABELS)
    su.to_csv(OUT / "setups.csv", index=False)
    print(f"{len(su)} continuation-CISD triggers "
          f"({su['trigger_rank'].eq(1).sum()} first-of-breakout)")

    # --- grid: depth bucket x stop x target --------------------------------
    grid_rows = []
    pops = [("all depths", su)] + [(lbl, su[su["depth_b"] == lbl]) for lbl in DEPTH_LABELS]
    for pop_name, d in pops:
        for stop in ["leg", "trig"]:
            for tgt in TARGETS:
                r = d[f"{stop}_r_{tgt}"].dropna()
                if len(r) < 30:
                    continue
                grid_rows.append({
                    "depth": pop_name, "stop": {"leg": "leg low", "trig": "trigger candle"}[stop],
                    "target": tgt, "n": len(r),
                    "P(target)": d[f"{stop}_hit_{tgt}"].mean(),
                    "P(stop first)": (d[f"{stop}_stopped_first"] & ~d[f"{stop}_hit_{tgt}"]).mean(),
                    "win_rate": (r > 0).mean(), "avg_R": r.mean(),
                    "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
                    "median_risk_pts": d.loc[r.index, f"{stop}_risk_pts"].median(),
                })
    grid = pd.DataFrame(grid_rows).round(3)
    grid.to_csv(OUT / "grid.csv", index=False)

    # --- direct stop comparison in the original 25-50% zone -----------------
    z = su[su["depth_b"] == "25-50%"]
    comp_rows = []
    for stop in ["leg", "trig"]:
        for tgt in TARGETS:
            r = z[f"{stop}_r_{tgt}"].dropna()
            comp_rows.append({
                "stop": {"leg": "leg low", "trig": "trigger candle"}[stop], "target": tgt,
                "n": len(r), "P(target)": z[f"{stop}_hit_{tgt}"].mean(),
                "win_rate": (r > 0).mean(), "avg_R": r.mean(),
                "profit_factor": r[r > 0].sum() / max(1e-9, -r[r < 0].sum()),
                "median_risk_pts": z.loc[r.index, f"{stop}_risk_pts"].median(),
            })
    comp = pd.DataFrame(comp_rows).round(3)
    comp.to_csv(OUT / "stop_comparison_25_50.csv", index=False)

    charts(su, grid)
    report(su, grid, comp)
    print(f"done -> {OUT}/")


def charts(su, grid):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    for ax, tgt, title in zip(axes, ["ext_025", "ext_05"],
                              ["target: extreme +0.25 DR", "target: extreme +0.5 DR"]):
        t = grid[(grid["target"] == tgt) & (grid["depth"] != "all depths")]
        piv = t.pivot(index="depth", columns="stop", values="avg_R").reindex(DEPTH_LABELS)
        piv.plot.bar(ax=ax, rot=0)
        ax.axhline(0, color="k", lw=1)
        ax.set_title(title)
        ax.set_ylabel("avg R per trade")
        ax.set_xlabel("pullback depth at trigger")
    fig.suptitle(f"{TF}m continuation CISD: expectancy by depth and stop placement")
    fig.tight_layout()
    fig.savefig(OUT / "grid_avg_r.png")

    fig, ax = plt.subplots(figsize=(7, 4.5))
    su["depth_at_trigger"].clip(upper=1).hist(bins=40, ax=ax)
    for q in (0.25, 0.5, 0.75):
        ax.axvline(q, color="r", ls="--", lw=1)
    ax.set_xlabel("pullback depth at trigger (fraction of dealing range)")
    ax.set_ylabel("triggers")
    ax.set_title("Where continuation CISDs fire (origin never violated)")
    fig.tight_layout()
    fig.savefig(OUT / "depth_hist.png")
    plt.close("all")


def report(su, grid, comp):
    md = lambda t: t.to_markdown(index=False)
    first = su[su["trigger_rank"] == 1]
    lines = [
        f"# {TF}m Continuation CISDs: any depth, leg-low vs trigger-candle stop",
        "",
        f"{len(su)} triggers across {su['date'].nunique()} sessions "
        f"({len(first)} first-of-breakout; later triggers come from deeper lows "
        "of the same episode or fresh episodes after new extremes). A trigger is "
        "valid at any pullback depth provided the retracement never violates the "
        "dealing-range origin; once the origin breaks, scanning stops for that "
        "breakout.",
        "",
        f"Median depth at trigger: {su['depth_at_trigger'].median():.2f}; "
        f"median risk: leg-low stop {su['leg_risk_pts'].median():.1f} pts, "
        f"trigger-candle stop {su['trig_risk_pts'].median():.1f} pts.",
        "",
        "## Stop comparison in the original 25-50% zone",
        "", md(comp), "",
        "## Full grid: depth x stop x target",
        "", md(grid), "",
        "![grid](grid_avg_r.png)",
        "![depth](depth_hist.png)",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
