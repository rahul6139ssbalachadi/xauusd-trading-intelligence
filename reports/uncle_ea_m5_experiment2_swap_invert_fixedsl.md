================================================================================
         SIMPLE_STRATEGY_FIXED_M5  -  EXPERIMENT REPORT #2
        SWAPPED SL/TP  +  INVERTED SIGNAL  +  FIXED STOP, NO TRAILING
================================================================================
  Prepared    : 2026-09-26
  Requested by: user -- "exchange the tp and sl, at R:R 1.0 put sl where tp
                 was and tp where sl was", then "remove the trailing, make it
                 a fixed sl", then "give both the report"
  Data        : db/trading.db, XAUUSD M5, 2026-05-01 .. 2026-08-31 (broker time)
  Harness     : research/uncle_ea_diagnose.py  (--compare, --variant, fixed_sl_pips)
  Tests       : 452 passed, 0 failures (29 in tests/test_uncle_ea_diagnose.py)

  VERDICT: ALL THREE VARIANTS LOSE. ONE NARROW CONFIGURATION REACHES PF 1.24
           BUT DIES AT 1.5x SPREAD ON 126 TRADES -- NOT DEPLOYABLE.


================================================================================
0. FIRST, A CORRECTION TO THE PREVIOUS REPORT
================================================================================

  In report #1 I wrote that the EA's break-even stop was not credited. I also
  listed trailing as if it were modelled. To be precise about what the
  harness has ever done:

  - TRAILING WAS NEVER MODELLED AT ALL. InpEnableTrailing had no effect in
    any run reported so far. So "remove the trailing" was already the state
    of the previous results -- this instruction changes no prior number.
  - WHAT IS NEW HERE IS THE FIXED STOP. A fixed pip stop replaces the
    1.5 x ATR multiple, which is the real change in this experiment.
  - THE BREAK-EVEN MOVE IS STILL NOT MODELLED. After TP1 the real EA moves
    the stop to break-even; the harness charges those trades -0.5 R. This
    remains a conservative bias, quantified in section 5.


================================================================================
1. THE THREE VARIANTS, SIDE BY SIDE
================================================================================

  All: fixed 0.01 lot, $10,000 start, spread 30p + 5p/side, 50% out at 0.5R.
  No trailing in any variant (it was never modelled).

  --- A. AS IS  (original: RR 1.1, SL 1.5xATR, TP 1.65xATR) ------------------
  month         n   win%     PF    avgR      net$    ret%  avgStop   medMFE
  2026-05     689   27.7   0.17  -0.338    -4.52   -0.05      80     0.36
  2026-06     662   28.7   0.21  -0.334    -4.36   -0.04      88     0.36
  2026-07     684   26.3   0.13  -0.374    -4.45   -0.04      70     0.34
  2026-08     640   25.8   0.13  -0.364    -4.26   -0.04      73     0.37
  TOTAL      2683   27.1   0.16  -0.353   -17.63   -0.18      78     0.36

  --- B. SWAPPED  (RR 1.0, SL and TP levels exchanged: SL 1.0xATR) ---------
  month         n   win%     PF    avgR      net$    ret%  avgStop   medMFE
  2026-05    1314   23.9   0.07  -0.341    -7.44   -0.07      52     0.26
  2026-06    1223   24.8   0.10  -0.357    -7.05   -0.07      56     0.26
  2026-07    1365   14.9   0.04  -0.457    -8.11   -0.08      45     0.06
  2026-08    1180   17.2   0.06  -0.402    -6.85   -0.07      48     0.16
  TOTAL      5094   20.1   0.07  -0.390   -29.51   -0.30      50     0.17

  --- C. INVERTED  (RR 1.0, trade direction REVERSED) -----------------------
  month         n   win%     PF    avgR      net$    ret%  avgStop   medMFE
  2026-05    1253   32.2   0.13  -0.110    -5.55   -0.06      52     0.65
  2026-06    1189   35.7   0.16  -0.090    -5.16   -0.05      57     0.73
  2026-07    1288   25.5   0.08  -0.116    -5.57   -0.06      45     0.62
  2026-08    1101   28.5   0.12  -0.079    -4.66   -0.05      49     0.75
  TOTAL      4843   30.4   0.12  -0.099   -20.99   -0.21      51     0.69


================================================================================
2. WHAT THE SWAP ACTUALLY DID -- AND WHY IT MADE THINGS WORSE
================================================================================

  Mechanically, exchanging the levels at R:R 1.0 does two things:
     - the stop narrows from 1.5 x ATR to 1.0 x ATR  (78 -> 50 pips)
     - the target sits where the stop was, so it is now 1.0 x ATR away

  The stop narrowing is what kills it. Round-trip cost is 40 pips and does
  not shrink. So:

      variant    avg stop   cost as % of stop   med MFE
      as is          78            51.4 %         0.36 R
      swapped        50            79.6 %         0.17 R

  79.6 % of every stop is consumed by spread and slippage before the trade
  has a chance. Median MFE collapses from 0.36R to 0.17R -- with a target
  only 1R away and a stop 1R away, most trades die at the stop before price
  can travel a third of the required distance.

  Trade count also nearly DOUBLES (2,683 -> 5,094). A tighter stop clears
  positions faster, freeing the single-position slot to re-enter more often.
  More trades at worse per-trade odds.

  Directional asymmetry explains the July collapse: sell signals cluster in
  downtrends, so in July a tighter sell stop was repeatedly hit while the
  July rally ran against those positions.


================================================================================
3. WHAT THE INVERSION ACTUALLY SHOWED -- A REAL PARTIAL SIGNAL
================================================================================

  Flipping the direction is the one change that moved expectancy materially:

      as is     avg R  -0.353    win 27.1 %   med MFE +0.36
      inverted  avg R  -0.099    win 30.4 %   med MFE +0.69

  Median MFE nearly DOUBLED, from 0.36R to 0.69R. Price does travel further
  in the opposite direction of these candle patterns than in the direction
  the EA trades them. That is a genuine, measurable finding: the reversal
  patterns are not merely uninformative, they are mildly BACKWARDS on this
  data.

  But inverted is still net NEGATIVE in all four months. A 69th-percentile
  favourable excursion is not enough to pay a 1.1R target against a stop
  that is hit first.


================================================================================
4. FIXED STOP + INVERTED -- THE ONLY CONFIGURATION THAT REACHES PF > 1
================================================================================

  Removing the ATR stop and using a FIXED pip stop, with the direction
  inverted and R:R 1.0:

    SL pips      n   win%     PF   avgR     net$   cost % of stop
       20     8276    0.0   0.00  -0.362  -39.09        200.0 %
       30     7164    0.0   0.00  -0.234  -33.68        133.3 %
       40     5838    0.0   0.00  -0.137  -26.55        100.0 %
       50     4730   43.7   0.09  -0.078  -20.76         80.0 %
       60     3790   45.7   0.18  -0.030  -15.85         66.7 %
       80     2597   49.3   0.35   0.047   -9.40         50.0 %
      100     1836   52.0   0.52   0.110   -5.33         40.0 %
      150      980   54.4   0.81   0.171   -1.40         26.7 %
      200      604   54.3   0.95   0.179   -0.25         20.0 %
      300      291   50.9   0.95   0.111   -0.20         13.3 %
      400      186   51.1   1.08   0.135    0.26         10.0 %
      500      126   54.0   1.24   0.175    0.60          8.0 %
      700       67   59.7   1.57   0.258    0.94          5.7 %
     1000       37   51.4   1.14   0.098    0.21          4.0 %

  The mechanism is the one identified in report #1, now isolated. A 500-pip
  stop makes the fixed 40 pips of cost only 8 % of the risk instead of 51 %.
  Cost is not being out-earned; it is being made irrelevant. As the stop
  widens, trades stop paying a round trip and start behaving like a
  direction bet: win rate converges on the ~54 % that a 1:1 payoff needs.

  Same sweep with the ORIGINAL direction stays negative at every width
  (best PF 0.45 at 200 pips). So the widening alone is not enough -- the
  inversion is doing real work. Both changes are required.

  R:R interaction at SL 200 (inverted):
        RR 1.0  PF 0.95     RR 1.5  PF 0.87
        RR 2.0  PF 0.73     RR 3.0  PF 0.71
  Widening the target only loses trades. 1:1 is correct here.


================================================================================
5. WHY THE PROMISING CELL IS STILL NOT DEPLOYABLE
================================================================================

  The best cell is inverted / fixed 500-pip stop / RR 1.0, at PF 1.24. It was
  put through the project's own robustness gates. It fails three of four.

  5.1  SAMPLE SIZE
       n = 126 trades across FOUR MONTHS. Roughly 31 per month. CLAUDE.md
       section 15 rejects "extremely low trade counts." A PF of 1.24 on 126
       samples has a standard error near 0.15, so the 95 % confidence
       interval includes 1.0 comfortably. This is not a statistically
       established result.

  5.2  SPREAD STRESS -- THE DECISIVE FAILURE
           spread        PF      net $
          30p (base)   1.24     +0.60
          45p (1.5x)    0.94     -0.18
          60p (2.0x)    0.97     -0.09
          90p (3.0x)    0.69     -0.98
       The entire edge is smaller than a 50 % increase in spread. Gold
       spreads widen routinely at news and the session open. The same
       failure killed the V12 BTC study in this project. An edge that only
       exists at the exact recorded cost is not an edge.

  5.3  PARAMETER STABILITY -- CLAUDE.md section 15
       Neighbouring stops, inverted, RR 1.0:
          150p 0.81   175p 0.94   190p 0.88   200p 0.95
          210p 0.96   225p 0.97   250p 0.89   275p 0.89   300p 0.95
       PF above 1.0 appears only at ISOLATED points (400p, 500p, 700p) with
       sub-1.0 values immediately either side. The spec requires a robust
       REGION, not a single favourable cell. 500 pips is a spike, not a
       plateau.

  5.4  PER-MONTH CONSISTENCY -- PARTIAL PASS
           2026-05   n=31   PF 1.32   +$0.19
           2026-06   n=38   PF 1.15   +$0.12
           2026-07   n=29   PF 0.94   -$0.03
           2026-08   n=31   PF 2.50   +$0.58
       Three of four months positive, but July negative and the August
       PF of 2.50 rests on 31 trades -- roughly four winners' difference
       between profit and loss. Walk-forward halves: May-Jun PF 1.33,
       Jul-Aug PF 1.10, i.e. degradation, though both nominally positive.

  5.5  THE UNMODELLED BREAK-EVEN MOVE CUTS THE OTHER WAY
       The harness still charges -0.5 R to the 443 partial-then-stop trades
       instead of roughly 0. Crediting it would lift the as_is average from
       -0.353 R to -0.270 R. On the promising cell the same credit applies,
       so the true result is somewhat BETTER than 1.24 -- but a bias of
       that size cannot rescue a cell that already fails spread stress.

  5.6  DOLLARS ARE TRIVIAL
       +$0.60 over four months at 0.01 lot. The cell is interesting as
       EVIDENCE, not as something to trade. To make $0.60 of edge pay a
       meaningful return the position size would have to be scaled by
       hundreds of times, at which point the 8 %-of-stop cost assumption
       stops being the binding constraint and slippage on a 500-pip stop
       becomes the real one.


================================================================================
6. WHAT I FOUND IN MY OWN CODE WHILE DOING THIS
================================================================================

  An R:R sweep initially returned IDENTICAL profit factors (0.95) at
  RR 1.0, 1.5, 2.0 and 3.0. That is impossible for a real sweep and was a
  genuine bug: `rr = rr or RR` ran before the variant's RR-1.0 branch,
  making that branch unreachable, so an explicit rr was always discarded
  and every variant silently ran at RR 1.1. Fixed, and the corrected sweep
  in section 4 is what produced the R:R interaction table.

  Regression test:
    tests/test_uncle_ea_diagnose.py::TestVariants::
        test_explicit_rr_overrides_the_variant_default

  I am flagging this because the first version of section 4's R:R table
  would have supported a false claim about reward ratios. It was caught
  before it reached a conclusion, and it is now pinned by a test.

  A second, smaller issue: several tests in the same file were passing
  VACUOUSLY because the flat synthetic fixture produced no candle patterns
  and therefore no trades, so assertions inside the loop never executed.
  The fixture now uses a zigzag and every trade-iterating test asserts
  non-emptiness first.


================================================================================
7. BOTTOM LINE
================================================================================

  Your three hypotheses, tested:

    1. "The R:R was wrong"                    REJECTED. RR 1.0 through 3.0
                                               and every ATR multiple from
                                               1.0x to 6.0x were tested. The
                                               original loses at all of them.

    2. "The SL and TP were backwards"          PARTIALLY CONFIRMED. Inverting
                                               the direction nearly doubles
                                               median favourable excursion
                                               (0.36R -> 0.69R). The candle
                                               patterns ARE mildly predictive
                                               in reverse. It still loses
                                               money as traded, because the
                                               1.1R target is unreachable
                                               with a stop that tight.

    3. "Use a fixed stop"                      NECESSARY BUT NOT SUFFICIENT.
                                               A fixed stop only works once
                                               it is wide enough that the 40
                                               pips of cost become
                                               irrelevant. That requires
                                               roughly 400+ pips, and at
                                               that width only the inverted
                                               direction shows any edge.

  RECOMMENDATION: DO NOT DEPLOY.

  The inversion finding is real and worth keeping -- it is the first time
  any version of this signal family has produced positive expectancy on
  this data, and it says the signal is backwards rather than absent. But
  the configuration that extracts it rests on 126 trades, dies at 1.5x
  spread, and sits on an isolated spike rather than a stable region. Under
  this project's own rules that is a REJECT, and I would not approve it
  for the demo account either.

  IF you want to pursue it, the only honest next step is more data, not
  more tuning. Specifically: 6+ months minimum to push n above 400, then
  re-run spread stress at 1.5x as a hard gate, and require PF above 1.0
  across a contiguous stop range rather than at isolated points. If it
  survives that, it is worth a paper run. If it does not, this family is
  closed.

  Note the wider context: this is the 17th strategy family tested on this
  dataset. Every M5/M15 family returns PF 0.00-0.36. The only survivors
  remain V11 (D1) and V12 (H1), both already running on the demo account.
================================================================================
