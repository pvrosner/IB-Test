"""Candle-character study: which 6am/7am hourly candles mean-revert to equilibrium?

Hypothesis under test: indecisive/consolidating candles produce mean reversion
back to the candle midpoint after a break; expansion candles produce further
expansion with shallow retracements.

Features per hourly candle (6-7am, 7-8am ET):
  rel_range     : range / trailing 20-day median range of the SAME hour slot
  body_ratio    : |close - open| / range
  close_dist    : |close - mid| / (range/2)  (0 = closes at equilibrium, 1 = at extreme)
  exp_ratio     : range / previous hour's range
  ctype         : 'consolidation' (rel_range bottom tercile & body_ratio < 0.5),
                  'expansion' (top tercile & body_ratio >= 0.5), else 'mixed'

Outcomes in the 120 minutes after the candle completes (1m bars):
  broke         : any trade beyond the candle high/low
  reverted      : after the first break, price returned to the candle midpoint
  reverted_first: returned to mid BEFORE extending 0.5x range past the broken edge
  max_ext       : deepest excursion past the broken edge, in range units
  max_retrace   : deepest pullback from the broken edge back into the range, in
                  range units (0 = never back inside, 0.5 = mid, 1 = full traverse)

Also: first-order Markov transition matrix of candle types (5am->6am->7am) and
whether the prior hour's type adds information.

Usage: python run_candle_character.py [path-to-parquet]
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
OUT = Path("results/candle_character")
OUT.mkdir(parents=True, exist_ok=True)

SLOTS = (5, 6, 7)          # hourly candles built; outcomes for 6 and 7
WINDOW = 120               # minutes after candle close


def build(df):
    df = df.assign(min=df.index.hour * 60 + df.index.minute)
    cand_rows, out_rows = [], []
    for date, day in df.groupby(df.index.date):
        candles = {}
        for s in SLOTS:
            cb = day[(day["min"] >= s * 60) & (day["min"] < (s + 1) * 60)]
            if len(cb) < 50:
                continue
            candles[s] = dict(o=cb["open"].iloc[0], h=cb["high"].max(),
                              l=cb["low"].min(), c=cb["close"].iloc[-1])
        for s in SLOTS:
            if s not in candles:
                continue
            cd = candles[s]
            rng = cd["h"] - cd["l"]
            if rng <= 0:
                continue
            row = {"date": date, "slot": s, **cd, "range": rng,
                   "body_ratio": abs(cd["c"] - cd["o"]) / rng,
                   "close_dist": abs(cd["c"] - (cd["h"] + cd["l"]) / 2) / (rng / 2),
                   "exp_ratio": rng / (candles[s - 1]["h"] - candles[s - 1]["l"])
                                if s - 1 in candles and candles[s - 1]["h"] > candles[s - 1]["l"] else np.nan}
            cand_rows.append(row)
            if s == 5:
                continue
            # outcome path
            w = day[(day["min"] >= (s + 1) * 60) & (day["min"] < (s + 1) * 60 + WINDOW)]
            if len(w) < 90:
                continue
            h = w["high"].to_numpy()
            l = w["low"].to_numpy()
            mid = (cd["h"] + cd["l"]) / 2
            up = np.argmax(h > cd["h"]) if (h > cd["h"]).any() else -1
            dn = np.argmax(l < cd["l"]) if (l < cd["l"]).any() else -1
            if up < 0 and dn < 0:
                out_rows.append({"date": date, "slot": s, "broke": False})
                continue
            d = 1 if (dn < 0 or (up >= 0 and up <= dn)) else -1
            b = up if d > 0 else dn
            edge = cd["h"] if d > 0 else cd["l"]
            a_h, a_l = h[b:], l[b:]
            ext = (a_h.max() - edge) / rng if d > 0 else (edge - a_l.min()) / rng
            ext = max(0.0, ext)
            retr = max(0.0, ((edge - a_l.min()) if d > 0 else (a_h.max() - edge)) / rng)
            rev = retr >= 0.5
            # race: mid touch vs 0.5R extension
            mid_hit = (a_l <= mid) if d > 0 else (a_h >= mid)
            ext_lvl = edge + d * 0.5 * rng
            ext_hit = (a_h >= ext_lvl) if d > 0 else (a_l <= ext_lvl)
            t_mid = np.argmax(mid_hit) if mid_hit.any() else len(a_h)
            t_ext = np.argmax(ext_hit) if ext_hit.any() else len(a_h)
            out_rows.append({"date": date, "slot": s, "broke": True,
                             "break_dir": "up" if d > 0 else "down",
                             "reverted": bool(rev),
                             "reverted_first": bool(t_mid < len(a_h) and t_mid <= t_ext),
                             "max_ext": ext, "max_retrace": retr})
    return pd.DataFrame(cand_rows), pd.DataFrame(out_rows)


def main():
    print("loading data...")
    df = load_continuous(DATA)
    cand, outc = build(df)

    # features with trailing same-slot baseline
    cand = cand.sort_values(["slot", "date"])
    cand["med20"] = cand.groupby("slot")["range"].transform(
        lambda s: s.rolling(20, min_periods=10).median().shift(1))
    cand["rel_range"] = cand["range"] / cand["med20"]

    cd = cand[cand["slot"].isin([6, 7])].merge(outc, on=["date", "slot"], how="inner")
    cd = cd.dropna(subset=["rel_range"])

    # candle type
    terc = cd["rel_range"].quantile([1 / 3, 2 / 3])
    cd["ctype"] = np.select(
        [(cd["rel_range"] <= terc.iloc[0]) & (cd["body_ratio"] < 0.5),
         (cd["rel_range"] >= terc.iloc[1]) & (cd["body_ratio"] >= 0.5)],
        ["consolidation", "expansion"], default="mixed")
    for col, q in [("body_ratio", 3), ("rel_range", 3), ("close_dist", 3), ("exp_ratio", 3)]:
        cd[f"{col}_b"] = pd.qcut(cd[col], q, labels=["low", "mid", "high"])
    cd.to_csv(OUT / "candles.csv", index=False)

    br = cd[cd["broke"]]
    print(f"{len(cd)} candle-sessions, {len(br)} with a break within {WINDOW}m "
          f"({len(br)/len(cd):.0%})")

    def tab(d, cond):
        g = d.groupby(cond, observed=True)
        return pd.DataFrame({
            "n": g.size(),
            "P(break)": np.nan,
            "P(revert to mid | break)": g["reverted"].mean(),
            "P(revert before 0.5R ext)": g["reverted_first"].mean(),
            "P(ext >= 0.5R)": g["max_ext"].apply(lambda s: (s >= 0.5).mean()),
            "P(ext >= 1R)": g["max_ext"].apply(lambda s: (s >= 1.0).mean()),
            "median ext": g["max_ext"].median(),
            "median retrace": g["max_retrace"].median(),
        }).round(3)

    tables = {}
    base = tab(br, "slot")
    base["P(break)"] = cd.groupby("slot")["broke"].mean().round(3)
    tables["baseline_by_slot"] = base
    for cond in ["ctype", "body_ratio_b", "rel_range_b", "close_dist_b", "exp_ratio_b"]:
        tables[f"by_{cond}"] = tab(br, cond)

    # 3x3 grid: rel_range x body_ratio
    grid = br.groupby(["rel_range_b", "body_ratio_b"], observed=True).agg(
        n=("reverted", "size"), p_revert=("reverted", "mean"),
        p_ext1=("max_ext", lambda s: (s >= 1.0).mean())).round(3)
    tables["grid_relrange_x_body"] = grid

    # Markov: candle-type transitions hour -> hour
    cand_t = cand.dropna(subset=["rel_range"]).copy()
    t2 = cand_t["rel_range"].quantile([1 / 3, 2 / 3])
    cand_t["ctype"] = np.select(
        [(cand_t["rel_range"] <= t2.iloc[0]) & (cand_t["body_ratio"] < 0.5),
         (cand_t["rel_range"] >= t2.iloc[1]) & (cand_t["body_ratio"] >= 0.5)],
        ["consolidation", "expansion"], default="mixed")
    piv = cand_t.pivot_table(index="date", columns="slot", values="ctype", aggfunc="first")
    trans = []
    for a, b in [(5, 6), (6, 7)]:
        if a in piv.columns and b in piv.columns:
            t = pd.crosstab(piv[a], piv[b], normalize="index").round(3)
            t.index = [f"{a}am {i}" for i in t.index]
            trans.append(t)
    tables["markov_transitions"] = pd.concat(trans)

    # does the PRIOR hour's type add info on top of the current type?
    prior = piv.rename(columns={5: "p5", 6: "p6", 7: "p7"})
    br6 = br[br["slot"] == 6].merge(prior["p5"].rename("prior_type"),
                                    left_on="date", right_index=True, how="left")
    br7 = br[br["slot"] == 7].merge(prior["p6"].rename("prior_type"),
                                    left_on="date", right_index=True, how="left")
    two = pd.concat([br6, br7]).dropna(subset=["prior_type"])
    tables["by_type_and_prior"] = (two.groupby(["ctype", "prior_type"], observed=True)
                                   .agg(n=("reverted", "size"), p_revert=("reverted", "mean"))
                                   .round(3))

    for name, t in tables.items():
        t.to_csv(OUT / f"{name}.csv")

    charts(br, cd)
    report(cd, br, tables)
    print("done -> results/candle_character/")


def charts(br, cd):
    plt.rcParams.update({"figure.dpi": 120, "axes.grid": True, "grid.alpha": 0.3})
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    for ax, (title, col) in zip(axes, [("P(revert to mid | break)", "reverted"),
                                       ("P(extension >= 1R | break)", None)]):
        if col:
            t = br.groupby(["slot", "ctype"], observed=True)[col].mean().unstack()
        else:
            t = br.groupby(["slot", "ctype"], observed=True)["max_ext"].apply(
                lambda s: (s >= 1.0).mean()).unstack()
        t.plot.bar(ax=ax, rot=0)
        ax.set_title(title)
        ax.set_xlabel("hour slot")
    fig.suptitle("Candle character vs post-break behavior (6am / 7am candles)")
    fig.tight_layout()
    fig.savefig(OUT / "type_outcomes.png")

    fig, ax = plt.subplots(figsize=(7, 5))
    sc = ax.scatter(br["rel_range"].clip(upper=3), br["body_ratio"],
                    c=br["reverted"].astype(int), cmap="RdYlGn_r", s=8, alpha=0.5)
    ax.set_xlabel("relative range (vs 20-day same-slot median)")
    ax.set_ylabel("body / range")
    ax.set_title("Green = extended, red = reverted to mid")
    fig.tight_layout()
    fig.savefig(OUT / "scatter.png")
    plt.close("all")


def report(cd, br, tables):
    md = lambda t: t.to_markdown()
    lines = [
        "# Candle Character and Mean Reversion to Equilibrium (6am / 7am candles)",
        "",
        f"{len(cd)} candle-sessions; {len(br)} broke within {WINDOW} minutes of "
        "completing. All outcome stats condition on a break having occurred. "
        "'Revert' = price returned to the candle midpoint after the first break; "
        "'revert before 0.5R ext' = the midpoint traded before price extended "
        "half a range beyond the broken edge (the race that matters for fades).",
        "",
    ]
    for name, t in tables.items():
        lines += [f"## {name}", "", md(t), ""]
    lines += ["![types](type_outcomes.png)", "![scatter](scatter.png)"]
    (OUT / "REPORT.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
