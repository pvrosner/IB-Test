"""Per-day event detection: IB stats, breakouts, displacement, FVG, CISD, outcomes.

Conventions
-----------
* All breakout logic is signed by ``sign`` (+1 bull breakout of IB high,
  -1 bear breakout of IB low) so one code path handles both directions.
* Distances are expressed in IB-range units ("R" = IB high - IB low).
* A breakout is the first 1m CLOSE beyond the IB edge after 10:30 ET.
* Displacement ratio = breakout bar body / average body of the prior 20 bars.
* FVG: standard 3-candle imbalance whose middle bar lies within
  [breakout-1, breakout+3], in the breakout direction.
* CISD (change in state of delivery): the breakout impulse is the run of
  consecutive same-direction closes ending at the breakout bar; CISD fires
  when a later bar CLOSES through the open of the first candle of that run.
* Same-bar ambiguity in strategy sims is resolved against the trade
  (stop assumed hit first).
"""

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

EXTENSIONS = (0.25, 0.5, 1.0, 1.5, 2.0)


@dataclass
class BreakoutEvent:
    date: object
    direction: str            # 'up' or 'down'
    is_first: bool            # first breakout of the day
    minutes_after_ib: int     # minutes after 10:30 ET
    close_through: float      # penetration of close beyond edge, in R
    disp_ratio: float         # breakout body vs avg body of prior 20 bars
    impulse_bars: int
    opposite_touched_first: bool
    fvg_present: bool
    fvg_size: float = np.nan  # in R
    # outcomes
    mfe: float = 0.0          # max excursion beyond edge after breakout, in R
    ext_reached: dict = field(default_factory=dict)
    retest: bool = False
    retest_minutes: float = np.nan
    resumed_after_retest: bool = False
    failed: bool = False      # closed back inside IB (any 1m close, noisy)
    failed_minutes: float = np.nan
    failed_before_05: bool = False  # closed back inside before ever reaching the 0.5R extension
    cisd: bool = False
    cisd_minutes: float = np.nan
    reached_mid_after: bool = False
    reached_opposite_after: bool = False
    cisd_then_mid: bool = False
    cisd_then_opposite: bool = False
    fvg_revisited: bool = False
    fvg_held: bool = False    # revisited and then reached 0.5R ext without closing through far side
    eod_beyond_edge: bool = False
    # strategy sims (R-multiples, risk = entry to stop)
    s1_breakout_r: float = np.nan
    s2_retest_filled: bool = False
    s2_retest_r05: float = np.nan
    s2_retest_r10: float = np.nan
    s3_cisd_fade_r: float = np.nan
    # day context (filled by caller)
    ib_range: float = np.nan
    ib_range_pctile: float = np.nan
    gap_dir: str = ""
    dow: int = -1


@dataclass
class DayRecord:
    date: object
    dow: int
    ib_open: float
    ib_high: float
    ib_low: float
    ib_range: float
    gap_points: float
    pattern: str              # 'none', 'up_only', 'down_only', 'both_up_first', 'both_down_first'
    first_touch: str          # 'high', 'low', 'none'
    eod_close_loc: float      # (close - ib_low) / range; >1 above IB, <0 below
    day_max_ext_up: float
    day_max_ext_down: float


def _find_fvg(h, l, lo_bar, hi_bar, sign):
    """Scan middle bars in [lo_bar, hi_bar] for a 3-candle FVG; return size or nan."""
    best = np.nan
    for m in range(max(lo_bar, 1), min(hi_bar, len(h) - 2) + 1):
        if sign > 0 and l[m + 1] > h[m - 1]:
            gap = l[m + 1] - h[m - 1]
        elif sign < 0 and h[m + 1] < l[m - 1]:
            gap = l[m - 1] - h[m + 1]
        else:
            continue
        if np.isnan(best) or gap > best:
            best = gap
    return best


def _fvg_zone(h, l, lo_bar, hi_bar, sign):
    """Return (top, bottom) of the largest FVG found, or None."""
    best, zone = 0.0, None
    for m in range(max(lo_bar, 1), min(hi_bar, len(h) - 2) + 1):
        if sign > 0 and l[m + 1] > h[m - 1] and l[m + 1] - h[m - 1] > best:
            best, zone = l[m + 1] - h[m - 1], (l[m + 1], h[m - 1])
        elif sign < 0 and h[m + 1] < l[m - 1] and l[m - 1] - h[m + 1] > best:
            best, zone = l[m - 1] - h[m + 1], (l[m - 1], h[m + 1])
    return zone


def _sim_long(o, h, l, c, entry_bar, entry_px, stop, target, sign):
    """R-multiple of a trade from entry_bar (entered at entry_px) to EOD.

    sign=+1 long, sign=-1 short. Same-bar stop+target counts as stop.
    """
    risk = (entry_px - stop) * sign
    if risk <= 0:
        return np.nan
    for i in range(entry_bar, len(c)):
        hit_stop = l[i] <= stop if sign > 0 else h[i] >= stop
        hit_tgt = h[i] >= target if sign > 0 else l[i] <= target
        if hit_stop:
            return -1.0
        if hit_tgt:
            return (target - entry_px) * sign / risk
    return (c[-1] - entry_px) * sign / risk


def analyze_day(date, ib: pd.DataFrame, post: pd.DataFrame, prev_close: float):
    """Return (DayRecord, [BreakoutEvent, ...]) for one session."""
    ib_open = ib["open"].iloc[0]
    ib_high = ib["high"].max()
    ib_low = ib["low"].min()
    rng = ib_high - ib_low
    mid = (ib_high + ib_low) / 2
    dow = pd.Timestamp(date).dayofweek

    # full-day arrays (IB + post) so displacement/impulse can look back into the IB
    day = pd.concat([ib, post])
    o = day["open"].to_numpy()
    h = day["high"].to_numpy()
    l = day["low"].to_numpy()
    c = day["close"].to_numpy()
    n_ib = len(ib)
    n = len(day)
    bodies = np.abs(c - o)

    # ---- day-level facts -------------------------------------------------
    ph = h[n_ib:]
    pl = l[n_ib:]
    pc = c[n_ib:]
    touch_up = np.argmax(ph >= ib_high) if (ph >= ib_high).any() else -1
    touch_dn = np.argmax(pl <= ib_low) if (pl <= ib_low).any() else -1
    if touch_up < 0 and touch_dn < 0:
        first_touch = "none"
    elif touch_dn < 0 or (touch_up >= 0 and touch_up <= touch_dn):
        first_touch = "high"
    else:
        first_touch = "low"

    bo_up = np.argmax(pc > ib_high) if (pc > ib_high).any() else -1
    bo_dn = np.argmax(pc < ib_low) if (pc < ib_low).any() else -1
    if bo_up < 0 and bo_dn < 0:
        pattern = "none"
    elif bo_dn < 0:
        pattern = "up_only"
    elif bo_up < 0:
        pattern = "down_only"
    else:
        pattern = "both_up_first" if bo_up < bo_dn else "both_down_first"

    gap = ib_open - prev_close if prev_close is not None else np.nan
    day_rec = DayRecord(
        date=date, dow=dow, ib_open=ib_open, ib_high=ib_high, ib_low=ib_low,
        ib_range=rng, gap_points=gap, pattern=pattern, first_touch=first_touch,
        eod_close_loc=(pc[-1] - ib_low) / rng,
        day_max_ext_up=max(0.0, (ph.max() - ib_high) / rng),
        day_max_ext_down=max(0.0, (ib_low - pl.min()) / rng),
    )

    # ---- breakout events -------------------------------------------------
    events = []
    breaks = []
    if bo_up >= 0:
        breaks.append((bo_up, +1))
    if bo_dn >= 0:
        breaks.append((bo_dn, -1))
    breaks.sort()

    for rank, (bo, sign) in enumerate(breaks):
        i = n_ib + bo  # index into full-day arrays
        edge = ib_high if sign > 0 else ib_low
        opp = ib_low if sign > 0 else ib_high

        # displacement
        prior = bodies[max(0, i - 20):i]
        avg_body = prior.mean() if len(prior) else np.nan
        disp = bodies[i] / avg_body if avg_body and avg_body > 0 else np.nan

        # impulse run of same-direction closes ending at the breakout bar
        j = i
        while j > 0 and np.sign(c[j - 1] - o[j - 1]) == sign and j > i - 30:
            j -= 1
        impulse_open = o[j]
        impulse_bars = i - j + 1

        # FVG near the breakout
        fvg_size = _find_fvg(h, l, i - 1, i + 3, sign)
        fvg = _fvg_zone(h, l, i - 1, i + 3, sign)

        ev = BreakoutEvent(
            date=date, direction="up" if sign > 0 else "down", is_first=(rank == 0),
            minutes_after_ib=int(bo),
            close_through=(c[i] - edge) * sign / rng,
            disp_ratio=disp, impulse_bars=impulse_bars,
            opposite_touched_first=(touch_dn >= 0 and touch_dn < bo) if sign > 0
                                   else (touch_up >= 0 and touch_up < bo),
            fvg_present=not np.isnan(fvg_size),
            fvg_size=fvg_size / rng if not np.isnan(fvg_size) else np.nan,
            ib_range=rng, gap_dir="" , dow=dow,
        )

        # ---- outcomes from bar i+1 to EOD --------------------------------
        a_h, a_l, a_c = h[i + 1:], l[i + 1:], c[i + 1:]
        fav = a_h if sign > 0 else a_l  # favorable extreme series
        adv = a_l if sign > 0 else a_h
        if len(a_c):
            bo_bar_ext = (h[i] - edge) / rng if sign > 0 else (edge - l[i]) / rng
            ev.mfe = max(0.0, ((fav * sign).max() - edge * sign) / rng, bo_bar_ext)
            for x in EXTENSIONS:
                lvl = edge + sign * x * rng
                ev.ext_reached[x] = bool(((a_h >= lvl).any() if sign > 0 else (a_l <= lvl).any())
                                         or (sign > 0 and h[i] >= lvl) or (sign < 0 and l[i] <= lvl))

            # retest of the edge
            rt = np.argmax(adv * sign <= edge * sign) if ((adv * sign) <= edge * sign).any() else -1
            if rt >= 0:
                ev.retest = True
                ev.retest_minutes = float(rt + 1)
                pre_ext = (max(fav[:rt].max(), h[i]) if sign > 0 else min(fav[:rt].min(), l[i])) if rt > 0 \
                    else (h[i] if sign > 0 else l[i])
                after = fav[rt:]
                ev.resumed_after_retest = bool((after * sign > pre_ext * sign).any())

            # failure: close back inside
            fl = np.argmax(a_c * sign < edge * sign) if ((a_c * sign) < edge * sign).any() else -1
            if fl >= 0:
                ev.failed = True
                ev.failed_minutes = float(fl + 1)
                lvl05 = edge + sign * 0.5 * rng
                if (sign > 0 and h[i] >= lvl05) or (sign < 0 and l[i] <= lvl05):
                    t05 = -1  # breakout bar itself reached 0.5R
                else:
                    r05 = (a_h >= lvl05) if sign > 0 else (a_l <= lvl05)
                    t05 = int(np.argmax(r05)) if r05.any() else n
                ev.failed_before_05 = fl < t05

            ev.reached_mid_after = bool(((a_l <= mid).any() if sign > 0 else (a_h >= mid).any()))
            ev.reached_opposite_after = bool(((a_l <= opp).any() if sign > 0 else (a_h >= opp).any()))

            # CISD: close through the impulse origin open
            cs = np.argmax(a_c * sign < impulse_open * sign) if ((a_c * sign) < impulse_open * sign).any() else -1
            if cs >= 0:
                ev.cisd = True
                ev.cisd_minutes = float(cs + 1)
                b_l, b_h = a_l[cs:], a_h[cs:]
                ev.cisd_then_mid = bool((b_l <= mid).any() if sign > 0 else (b_h >= mid).any())
                ev.cisd_then_opposite = bool((b_l <= opp).any() if sign > 0 else (b_h >= opp).any())

            # FVG revisit-and-hold (the "FVG trade")
            if fvg is not None:
                top, bot = fvg
                prox = a_l if sign > 0 else a_h
                tz = np.argmax(prox * sign <= top * sign) if ((prox * sign) <= top * sign).any() else -1
                if tz >= 0:
                    ev.fvg_revisited = True
                    lvl = edge + sign * 0.5 * rng
                    closed_thru = (a_c[tz:] * sign < bot * sign)
                    reached = (a_h[tz:] >= lvl) if sign > 0 else (a_l[tz:] <= lvl)
                    t_thru = np.argmax(closed_thru) if closed_thru.any() else n
                    t_reach = np.argmax(reached) if reached.any() else n
                    ev.fvg_held = bool(t_reach < n and t_reach <= t_thru)

            ev.eod_beyond_edge = bool(a_c[-1] * sign > edge * sign)

        # ---- strategy sims ------------------------------------------------
        # S1: enter at breakout close, stop at IB mid, target 1R extension
        if i + 1 < n:
            ev.s1_breakout_r = _sim_long(o, h, l, c, i + 1, c[i], mid, edge + sign * rng, sign)
        # S2: limit at the IB edge on a retest, stop at mid
        if ev.retest and len(a_c):
            fill_bar = i + 1 + int(ev.retest_minutes) - 1
            ev.s2_retest_filled = True
            ev.s2_retest_r05 = _sim_long(o, h, l, c, fill_bar, edge, mid, edge + sign * 0.5 * rng, sign)
            ev.s2_retest_r10 = _sim_long(o, h, l, c, fill_bar, edge, mid, edge + sign * rng, sign)
        # S3: fade the failed breakout on CISD, stop at post-breakout extreme, target opposite edge
        if ev.cisd and len(a_c):
            cs_bar = i + 1 + int(ev.cisd_minutes) - 1
            extreme = (max(h[i:cs_bar + 1].max(), h[i]) if sign > 0 else min(l[i:cs_bar + 1].min(), l[i]))
            if cs_bar + 1 < n:
                ev.s3_cisd_fade_r = _sim_long(o, h, l, c, cs_bar + 1, c[cs_bar], extreme, opp, -sign)

        events.append(ev)

    return day_rec, events


def events_to_frame(events) -> pd.DataFrame:
    rows = []
    for ev in events:
        d = asdict(ev)
        ext = d.pop("ext_reached")
        for x in EXTENSIONS:
            d[f"ext_{x}"] = ext.get(x, False)
        rows.append(d)
    return pd.DataFrame(rows)
