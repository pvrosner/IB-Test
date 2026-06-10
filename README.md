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

## Layout

```
ib_engine/data.py        load parquet, build continuous front month, slice RTH sessions
ib_engine/events.py      per-day detection: IB, breakouts, displacement, FVG, CISD, outcomes
ib_engine/probability.py conditional probability tables + strategy expectancies
run_backtest.py          orchestrates the study, writes results/
```
