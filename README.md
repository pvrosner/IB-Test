# NQ Initial Balance Probability Engine

A systematic backtest of how NQ price interacts with the **initial balance** (IB) — the
9:30–10:30 ET opening-hour range — incorporating ICT concepts: **displacement**,
**fair value gaps (FVG)** and **change in state of delivery (CISD)**.

Data: NQ futures 1m OHLCV (Databento), 2021-04-01 → 2026-04-27, volume-rolled
front month, 1,263 regular sessions, 1,195 first-breakout events.

## Running it

```bash
pip install pandas numpy pyarrow matplotlib tabulate
python run_backtest.py path/to/NQ_FUT_ohlcv_1m.parquet
```

Outputs go to `results/`: per-day and per-event CSVs, conditional probability
tables, charts, and `results/REPORT.md` (the full study).

## Definitions

| Term | Definition used here |
|---|---|
| IB | 9:30–10:30 ET high/low. All distances are in **R** = IB range. |
| Breakout | First 1m **close** beyond an IB edge after 10:30 ET. |
| Displacement | Breakout-bar body ÷ average body of the prior 20 bars (bucketed into quartiles). |
| FVG | 3-candle imbalance whose middle bar is within [breakout−1, breakout+3], in the breakout direction. |
| CISD | A later 1m close through the open of the candle run that delivered the breakout. |
| Failure | Any 1m close back inside the IB; `failed_before_05` requires it to happen before the +0.5R extension is reached. |

Strategy sims resolve same-bar stop/target ambiguity **against** the trade and
exit flat at the 16:00 close. No commissions/slippage are modeled.

## Key findings

**1. Almost every day picks a side.** 79% of sessions break out of one side only
(42% up, 37% down), 17% break both sides, and only 4% never close outside the IB.

**2. Breakouts extend, but modestly.** After the first breakout: 78% tag +0.25R,
58% reach +0.5R, 24% reach +1.0R, only 5% reach +2.0R. Median max excursion is
+0.59R beyond the edge.

**3. The "clean" breakout barely exists at 1m granularity.** 94% of breakouts
retest the edge, 90% close back inside the IB at least once, and 80% close back
inside *before* ever reaching +0.5R. But after a retest, 81% go on to make a new
extreme. Consequence: **limit entries at the edge beat chasing the breakout
close** — the retest-entry sim (S2, +0.5R target) wins 52% and earns +0.04R/trade
vs +0.03R for entering on the breakout close with the same stop, and rises to
**+0.07R/trade at 54% win rate when an FVG formed at the breakout**.

**4. Displacement mainly predicts *not failing*, not running further.** From the
weakest to strongest displacement quartile: CISD rate falls 76% → 67%, failure
before +0.5R falls 86% → 70%, and full rotation to the opposite side falls
23% → 15%. Extension odds improve only modestly (+1.0R: 18% → 27%).

**5. Time of day is the strongest conditioner.** Breakouts before 11:00 reach
+1.0R 27% of the time; after 13:30 only 7%. Early breakouts also revert fully
more often (23% vs 5%) — late breakouts simply run out of session.

**6. CISD is necessary but not sufficient for full rotation.** A CISD follows
73% of breakouts. Once it fires, 61% of breakouts rotate to the IB mid and 27%
reach the opposite side — versus ~0% reaching the opposite side without a CISD.
Fading a CISD all the way to the opposite edge is roughly breakeven (−0.02R);
it turns slightly positive (+0.04R) when the original breakout had weak
displacement.

**7. FVGs at the breakout are informative but routinely violated.** An FVG forms
at 79% of breakouts and improves continuation (failure 87% vs 98% without one;
+1.0R odds 24% vs 21%). But the textbook FVG trade is weak on 1m NQ: 94% of
breakout FVGs get revisited, and only 14% of those revisits hold the gap and
extend to +0.5R before price closes through the far side.

**8. Narrow IB days rotate; wide IB days stall (in R terms).** The narrowest IB
quintile reaches +1.0R 40% of the time but also hits the opposite side 34% of
the time; the widest quintile reaches +1.0R only 10% of the time. Mean-reversion
risk and extension potential both scale up as the IB narrows.

**9. Nothing here is a money printer.** The best unfiltered expectancies are
+0.03–0.09R per trade before costs. The edges that survive are *relative*:
retest entries > chase entries, FVG-confirmed > naked breakouts, early > late,
strong displacement > weak for continuation, weak displacement > strong for
fades.

See [`results/REPORT.md`](results/REPORT.md) for every table.

## Signal timeframe: 1m vs 5m vs 15m

`run_multitf.py` reruns the identical study with breakouts/displacement/FVG/CISD
defined on 5m and 15m closes (same 9:30–10:30 IB). Headline: **higher-timeframe
closes are a meaningful confirmation filter, at the cost of later and deeper
entries** (median close-through 0.03R → 0.11R, median breakout time 20 → 30 min):

- Failure before +0.5R falls 80% (1m) → 68% (5m) → 50% (15m); CISD rate falls 73% → 33%.
- A CISD becomes a real reversal signal at 15m: P(reach opposite side | CISD)
  rises 27% → 45%, and P(reach IB mid | CISD) 61% → 78%.
- The FVG playbook improves dramatically: revisited gaps hold and extend to
  +0.5R 14% (1m) → 25% (5m) → 38% (15m) of the time, and FVG presence separates
  +1R odds 31% vs 14% at 15m (vs 24% vs 21% at 1m).
- Displacement discriminates far better at 15m: +1R reach is 40% (strongest
  quartile) vs 20% (weakest); failure before +0.5R 27% vs 67%.
- Best sims in the study are 15m FVG-confirmed entries: S2 retest + FVG
  **+0.23R/trade, 62% win, PF 1.70** (n=683); S1 chase + FVG +0.18R, PF 1.57 —
  despite the conservative same-bar rule penalizing coarser bars hardest.

Full comparison: [`results/multitf/REPORT.md`](results/multitf/REPORT.md).

## 5m CISD fade with manipulation-leg projections

`run_cisd_fade.py` isolates the fade setup: price raids beyond an IB edge,
forms an extreme, then closes (5m) through the open of the run-up that made it
(CISD). Entry at the CISD close, stop at the raid extreme, targets projected
from the **bodies** of the manipulation leg (1.0x = full body-retrace of the
run; 2.0x = symmetric extension below it). ~1 setup per session (n=1,282).

- The CISD close itself already sits a median **1.29x** down the projection
  scale — 0.5x/1.0x are behind the entry by construction; real targets start at 1.5x.
- Delivery before stop/EOD: **1.5x 82%, 2.0x 68%, 2.5x 55%, 3.0x 45%, 4.0x 32%**
  (median deepest delivery 2.7x).
- **Sweep raids (wick beyond the edge, no 5m close through) fade far better than
  close-through raids**: 2.0x delivery 76% vs 66%, 3.0x 58% vs 43%, and only
  sweep fades are net positive as fixed-target trades (2.5x target: 61% hit,
  +0.06R, PF 1.29 vs PF 0.88 for close-through raids).

Full study: [`results/cisd_fade_5m/REPORT.md`](results/cisd_fade_5m/REPORT.md).

## Retracement entries after 5m close-through breakouts

`run_retrace.py` measures where close-through breakouts retrace to — on the
**dealing range** (session extreme-so-far → trailing post-breakout extreme) —
before resuming in the breakout direction (n=1,351; 94% print a new extreme
after the breakout bar).

- Before the *first* new extreme, 88% retrace **less than 25%** of the dealing
  range (median 0%) — immediate continuation is the norm.
- The deepest pullback absorbed before the day's *last* new extreme is most
  often in the **25–50% zone** (40% of continuers; median 28%, p75 43%). Only
  18% of continuing breakouts ever retrace past 50%.
- Anchored to the breakout impulse leg instead, the median deepest pullback is
  **60% of the leg** — right at the OTE zone (62–79%).
- Trailing-limit sims (stop at the 100% level, target the standing extreme):
  the 25% and 50% levels are mildly positive (PF ~1.1); the **75% level loses**
  (10% return to the extreme, 55% stop out). Edge concentrates in breakouts
  before 11:00 (50% level: +0.08R avg); after 12:00 retrace entries go negative.

Full study: [`results/retrace_5m/REPORT.md`](results/retrace_5m/REPORT.md).

## Continuation CISD in the 25–50% zone

`run_cont_cisd.py` chains the full sequence: close-through breakout → pullback
whose deepest depth sits in 25–50% of the dealing range (episodes exceeding 50%
are invalidated until a new extreme resets them) → 5m close back through the
opens of the retracement leg (continuation CISD) → entry at that close, **stop
at the CISD-leg low**. 640 setups (47% of close-through breakouts develop it).

- The confirmation works: 56% return to the standing extreme vs 28% for an
  unconfirmed 50%-limit fill, and only 37% stop out before any target.
- But the stop placement makes the old extreme a poor target: the CISD close
  already sits a median 0.64R from the extreme, so that target nets ~0R (PF 1.0).
- Expectancy lives in the extensions: extreme +0.25 DR averages **+0.11R (PF
  1.20)** and +0.5 DR **+0.15R (PF 1.27)**, paid for by 2–3R winners. Median MFE
  is 0.82R, arguing for scale-outs or a trail rather than the old high as target.
- Conditioning reverses the raw-limit time pattern: setups from breakouts after
  12:00 are the best (+0.36R at the +0.5 DR target), and deeper triggers
  (37.5–50%) beat shallow ones.

Full study: [`results/cont_cisd_5m/REPORT.md`](results/cont_cisd_5m/REPORT.md).

## Continuation CISDs at any depth, two stop placements

`run_cont_cisd_grid.py` widens the zone to **any pullback depth that never
violates the dealing-range origin** (origin break = structure dead, scanning
stops) and collects *all* continuation CISDs (3,262 triggers), simulating each
with two stops: the CISD-leg low and the **trigger-candle low**.

- **The sweet spot is the 50–75% pullback** (n=427): positive at every
  stop/target combination, best overall is the trigger-candle stop targeting
  the old extreme — **+0.28R avg, PF 1.47**, with median risk of only ~20 pts.
- Shallow CISDs (<25% depth, half of all triggers) are noise: negative with
  both stops. The 75–100% bucket is also negative — too close to full negation.
- In the original 25–50% zone, the trigger-candle stop roughly halves the risk
  (38 → 21 pts) and doubles expectancy on extension targets (+0.15R vs +0.07R
  at +0.5 DR), trading win rate for payoff.
- Pattern: depth + confirmation + intact origin = the deeper the pullback the
  market survives (up to ~75%), the better the continuation pays.

Full study: [`results/cont_cisd_grid_5m/REPORT.md`](results/cont_cisd_grid_5m/REPORT.md).

## Hourly-range fade models (6–7am / 7–8am candles, 1m entries)

`run_hourly_range.py` tests the Mc5calpAfee-style mean-reversion models: fade a
break of the 6am or 7am ET hourly candle back to the range midpoint at 1.3:1
reward:risk, entering on the retest (Breakout/Retest: close outside → close
back inside → retest; Breakout Failure: first breaking bar wicks out → retest),
skipping the trade if the midpoint trades before the retest. 3,588 pre-9:30
fills over 5 years.

- **Every model/range combo is breakeven-to-negative before costs**: win rates
  40.4–43.3% vs the 43.5% needed at 1.3:1 (avg R −0.004 to −0.072, PF 0.88–0.99).
- The failure model beats the retest model on the 6am range; the 7am range
  beats the 6am range. Best cell: 7am failure longs (+0.03R, n=307) — within
  noise of zero.
- Breakeven-after-25% management only helps the 7am retest (+0.018R, PF 1.05);
  it hurts everything else. Allowing fills after 9:30 makes all combos worse.
- Median stop is only 12–14 NQ points, so ~1 pt of round-trip cost ≈ 7% of
  risk per trade — comfortably wiping out even the best cells.

Full study: [`results/hourly_range_1m/REPORT.md`](results/hourly_range_1m/REPORT.md).

### Break-quality filters and equilibrium-zone targets

`run_hourly_range_sweep.py` adds penetration/displacement filters on the break
and a zone target forward of the exact midpoint. Median penetration past the
level is only **3.75 pts** (25th pct: 1.5 pts) — most breaks barely clear the
line. Requiring a real sweep flips the failure model positive:

- **7am failure + penetration ≥ 0.5 ATR: +0.09R, PF 1.18 (n=141)**, positive 5
  of 6 years; 6am equivalent +0.05–0.08R. Deeper fixed-point filters do better
  still (+0.15R, PF 1.30) but on thin samples (n≈58).
- Displacement ≥ 1.5 lifts the 7am retest model from −0.004R to **+0.023R
  (n=715)** — the largest-sample positive cell.
- The equilibrium zone (target 5–10% of range short of the mid) is roughly
  neutral: it trims a little from winners and saves a few near-misses; 5% is
  harmless, 10% gives up too much.

Full sweep: [`results/hourly_range_sweep/REPORT.md`](results/hourly_range_sweep/REPORT.md).

## Layout

```
ib_engine/data.py        load parquet, build continuous front month, slice RTH sessions
ib_engine/events.py      per-day detection: IB, breakouts, displacement, FVG, CISD, outcomes
ib_engine/probability.py conditional probability tables + strategy expectancies
run_backtest.py          orchestrates the study, writes results/
```
