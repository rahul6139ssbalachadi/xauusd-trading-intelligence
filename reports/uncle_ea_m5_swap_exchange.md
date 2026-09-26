================================================================================
            SIMPLE_STRATEGY_FIXED_M5  -  BACKTEST RESULT
              EXCHANGED (SWAPPED) SL / TP   --   FULL REPORT
================================================================================
  Prepared    : 2026-09-26
  Requested by: user -- "exchange the tp and sl and at r:r=1.0 put sl at the
                 place of tp and put tp at place of sl"
  Variant     : swap  --  RR 1.0, SL and TP levels exchanged
  Data        : db/trading.db, XAUUSD M5, 2026-05-01 .. 2026-08-31 (broker time)
  Money       : fixed 0.01 lot, $10,000 starting balance
  Costs       : spread 30 pips + 5 pips/side slippage = 40 pips round trip
  Partial     : 50 % closed at 0.5R
  Trailing   : NOT PRESENT (never modelled in this harness at all)
  Tests       : 452 passed, 0 failures

  VERDICT: WORSE THAN THE ORIGINAL. PF 0.07. 5,094 TRADES, ALL FOUR MONTHS
           NEGATIVE, AND IT COLLAPSES ENTIRELY UNDER SPREAD STRESS.


================================================================================
1. HEADLINE NUMBERS
================================================================================

    trades                    5,094
    win rate                    20.1 %
    profit factor                0.07
    average R per trade         -0.390 R
    net                         -$29.51   (-0.30 %)
    ending balance               $9,970.49
    average stop                 50 pips
    round-trip cost              40 pips  =  79.6 % of the stop
    median MFE                  +0.17 R
    mean MFE                    +0.55 R
    mean MAE                    -1.25 R
    trades never reaching +0.5R 3,050  (60.0 %)
    longs / shorts             3,687 / 1,407

  Compare with the original EA (report #1): PF 0.16, -0.353 R, 2,683 trades.
  The swap produces ALMOST DOUBLE the trades and roughly HALF the profit
  factor. It is not a marginal difference -- it is a materially worse system.


================================================================================
2. MONTH BY MONTH
================================================================================

  month         n   win%     PF    avgR      net$   avgStop  cost % of stop
  ---------------------------------------------------------------------------
  2026-05    1314   23.9   0.07  -0.341    -7.44      52p        76.7 %
  2026-06    1223   24.8   0.10  -0.357    -7.05      56p        71.0 %
  2026-07    1365   14.9   0.04  -0.457    -8.11      45p        89.0 %
  2026-08    1180   17.2   0.06  -0.402    -6.85      48p        82.9 %

  Not one profitable month. July is the worst at PF 0.04, where the stop
  narrowed to 45 pips and 89 % of it was consumed by cost before the trade
  could move.


================================================================================
3. WHY THE SWAP IS WORSE -- THE MECHANISM
================================================================================

  Exchanging the levels at RR 1.0 is not a neutral rearrangement. It does
  two things at once, and both are harmful.

  3.1  THE STOP NARROWS, THE COST DOES NOT
       Original: stop = 1.5 x ATR = 78 pips, target = 1.1 x that.
       Swapped : stop = 1.0 x ATR = 50 pips, target = 1.0 x stop.

       The 40 pips of spread and slippage are a CONSTANT. Halving the stop
       doubles their share of it:

           variant     avg stop    cost % of stop    median MFE
           as is          78 pips         51.4 %         0.36 R
           SWAPPED        50 pips         79.6 %         0.17 R

       79.6 % of every stop is spent on execution before the trade has any
       chance of working. Median favourable excursion HALVES. Price simply
       does not travel far enough to reach a 1R target once it must first
       clear 40 pips and stop 50 pips away.

  3.2  TIGHTER STOP -> NEARLY DOUBLE THE TRADES
       2,683 -> 5,094. A 50-pip stop clears faster than a 78-pip stop, so
       the single-position slot frees up sooner and the EA re-enters almost
       every second bar. The median trade now lasts ONE M5 bar.

       More trades, each with worse odds, each paying the full 40 pips.

  3.3  THE PAYOFF STRUCTURE
       At RR 1.0 with a 50 % partial at 0.5R:
           a winner  = +0.5R (partial) + 0.5R x 1.0 (runner) = +1.0R
           a loser    = -1.0R
           breakeven therefore needs a win rate ABOVE 50 %.

       Actual win rate: 20.1 %. The distribution has only three populated
       buckets and NOTHING between 0R and +1R:

           outcome              count    share
           <= -1R (stopped)     3,155    61.9 %
           -1R .. 0R             514    10.1 %
            0R .. +0.5R             0     0.0 %
           +0.5R .. +1R            0     0.0 %
           >= +1R (target)      1,425    28.0 %

       The middle is empty BY CONSTRUCTION: TP1 sits exactly at 0.5R, so a
       trade either takes the partial and then resolves at the stop or the
       target, or never gets there. There is no third outcome to soften
       the loss.

  3.4  BOTH DIRECTIONS LOSE EQUALLY BADLY
           BUY   n=3,687   win 21.1 %   PF 0.07   avgR -0.361   -$20.79
           SELL  n=1,407   win 17.4 %   PF 0.05   avgR -0.466    -$8.72
       Selling is worse than buying. This matters: it means the swap did not
       simply mis-time a trend. Shorting these signals loses MORE than
       longing them, which is the same backwards-leaning behaviour the
       inversion test exposed in report #2.


================================================================================
4. ROBUSTNESS -- THE SWAP FAILS EVERY GATE
================================================================================

  4.1  FIXED STOP INSTEAD OF ATR (still swapped, still no trailing)
       A wider stop reduces the cost share, so this is the swap's best hope.

        SL pips      n   win%     PF    avgR      net$   cost % stop
             50    4936   28.9   0.05  -0.372   -28.93       80.0 %
            100    1939   32.5   0.22  -0.274   -13.06       40.0 %
            200     611   37.8   0.45  -0.166    -4.47       20.0 %
            300     297   44.8   0.71  -0.026    -1.42       13.3 %
            500     120   41.7   0.76  -0.043    -0.74        8.0 %
            700      70   38.6   0.66  -0.130    -0.92        5.7 %
           1000      32   37.5   0.68  -0.129    -0.54        4.0 %

       PF approaches 1.0 as the stop widens, but NEVER crosses it, and it
       never turns net positive even at a 1,000-pip stop. Compare the
       INVERTED variant from report #2, which reached PF 1.24 at a 500-pip
       stop. The swap is structurally weaker: it gets to ~0.76 and stops.

       Note also that at 500 pips there are only 120 trades, and win rate
       FALLS from 44.8 % at 300 pips to 41.7 % at 500. It is not converging
       on a 50 % coin flip; it is running out of signals.

  4.2  SPREAD STRESS -- TOTAL COLLAPSE
        spread        PF        net $
          30p (base)   0.07     -$29.51
          45p (1.5x)    0.02     -$43.01
          60p (2.0x)    0.01     -$58.33
          90p (3.0x)    0.00     -$93.45

       This is the worst response of any variant tested in this project.
       At 1.5x spread the swap has already lost 99.3 % of every dollar it
       makes. Because the stop is only 50 pips, any spread widening eats
       the entire risk budget in a single trade.

  4.3  SUMMARY AGAINST THE PROJECT'S OWN GATES (CLAUDE.md sections 15, 18)
       sample size ............................ PASS  (5,094 trades)
       positive out-of-sample / per-month ..... FAIL  (0 of 4 months)
       profit factor > 1.0 .................... FAIL  (0.07)
       survives spread stress ................. FAIL  (0.02 at 1.5x)
       robust parameter region ............... FAIL  (monotonic decay)
       expectancy after costs ................. FAIL  (-0.390 R)

       Four of six fail. This is a clear REJECT.


================================================================================
5. WHAT THE SWAP ACTUALLY PROVED
================================================================================

  The swap answered the question it was designed to answer, and the answer
  is useful even though it is negative:

  5.1  THE ORIGINAL PROBLEM IS NOT "SL AND TP IN THE WRONG PLACE".
       Putting the stop where the target was and vice versa made things
       worse, not better. If the levels were simply mis-assigned, this
       would have fixed it. It did not.

  5.2  NARROWING THE STOP IS ACTIVELY HARMFUL ON M5 GOLD.
       40 pips of cost against a 50-pip stop is an unwinnable structure.
       The M5 timeframe does not support tight stops on this symbol at this
       spread. This is consistent with the R:R sweep in report #1, where
       every TP2 multiple from 1.0x to 6.0x ATR also lost.

  5.3  IT INDEPENDENTLY CORROBORATES THE INVERSION FINDING.
       Selling these signals loses MORE than buying them (PF 0.05 vs 0.07,
       avgR -0.466 vs -0.361). The candle patterns lean backwards. The
       dedicated inversion test in report #2 measured the same thing more
       precisely: median MFE rises from 0.36R to 0.69R when direction is
       reversed. Two independent experiments agreeing is meaningful.

  5.4  THE COST FLOOR IS THE BINDING CONSTRAINT, NOT THE SIGNAL.
       Every configuration, in every direction, at every stop width, at
       every R:R, has a profit factor below 1.0. The pattern is too
       consistent to be coincidence.


================================================================================
6. CAVEATS AND HONEST NOTES
================================================================================

  6.1  DOLLAR FIGURES UNDERSTATE THE DAMAGE, AS BEFORE.
       -$29.51 over four months looks survivable at 0.01 lot. It is not.
       The strategy loses 0.390 R on EVERY trade. At the corrected EA's
       2 %-of-balance sizing, that edge is a total account loss. Small lots
       were hiding the damage, not preventing it.

  6.2  THE BREAK-EVEN MOVE IS STILL NOT MODELLED.
       After TP1 the real EA moves the stop to break-even. The harness
       charges the 514 partial-then-stop trades a full -0.5 R instead of
       roughly 0. Crediting it would improve the average modestly. It
       cannot close a gap from 0.07 to above 1.0.

  6.3  SPREAD IS ASSUMED, NOT MEASURED PER BAR.
       30 pips + 5 pips/side is the recorded broker spread. If your live
       spread is wider, section 4.2 is the relevant table and it is worse.

  6.4  THIS WEEK'S DATA WAS NOT AVAILABLE.
       M5 history ends 2026-08-28. May-August is the most recent available
       stretch. To test a specific week once the terminal is reachable:
           ./.venv/Scripts/python.exe research/uncle_ea_diagnose.py \
               --monthly --variant swap --from 2026-09-21 --to 2026-09-26

  6.5  NO TRAILING WAS EVER MODELLED, IN THIS OR ANY PRIOR RUN.
       The swap is therefore also the "fixed SL, no trailing" configuration
       you asked for in the follow-up. The two requests are satisfied by
       this single run, and no earlier number changes.


================================================================================
7. CONCLUSION
================================================================================

  THE EXCHANGED SL/TP STRATEGY IS NOT VIABLE.

  PF 0.07 over 5,094 trades across four months, every month negative, and a
  profit factor of 0.02 the moment the spread widens by 50 %. The stop
  narrowing that the swap implies is the direct cause: it pushes round-trip
  cost to 79.6 % of the risk and halves the distance price travels before
  the stop takes it out.

  Combined with report #1, three of your three hypotheses about the loss
  are now tested and closed:

    1. "Wrong R:R" .............. REJECTED. All ratios lose.
    2. "SL and TP are backwards" . REJECTED as a fix (the swap is worse),
                                    but CONFIRMED as an observation --
                                    these signals are mildly inverted.
    3. "Fixed stop" ............. INSUFFICIENT on its own. Helps only when
                                    combined with inversion, and even then
                                    only at stop widths that fail spread
                                    stress.

  DO NOT DEPLOY ANY VERSION OF THIS EA. The candle-reversal signal has now
  been tested in three configurations and inverted, and the best result in
  this project remains report #2's inverted 500-pip cell at PF 1.24 -- which
  itself fails at 1.5x spread on 126 trades and is not deployable either.

  This is the 17th strategy family tested on this dataset. Every M5/M15
  family returns PF 0.00-0.36. The pattern across all of them is that the
  available edge on 5-minute gold is smaller than the cost of executing it.
  V11 (D1) and V12 (H1) remain the only survivors, and both are already
  running on your demo account.
================================================================================
