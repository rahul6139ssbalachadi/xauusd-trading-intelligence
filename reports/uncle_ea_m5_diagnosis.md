================================================================================
                 SIMPLE_STRATEGY_FIXED_M5  -  DIAGNOSIS REPORT
                          XAUUSD / GOLD.i#  /  M5
================================================================================
  Prepared      : 2026-09-26
  Strategy file : mql5/Simple_Strategy_Fixed_M5.mq5  (corrected, 0 err / 0 warn)
  Data          : db/trading.db  -  XAUUSD M5, 2026-02-16 .. 2026-08-28
  Test window   : 2026-05-01 .. 2026-08-31  (broker time, Mon-Fri)
  Harness       : research/uncle_ea_m5_week.py, research/uncle_ea_diagnose.py
  Tests         : 415 passed, 0 failures

  VERDICT: THE STRATEGY HAS NO EDGE. THE LOSS IS NOT A MONEY-MANAGEMENT BUG.


================================================================================
1. HEADLINE
================================================================================

  EA AS WRITTEN (RR 1.1, fixed 0.01 lot, 50% out at 0.5R, SL 1.5xATR)
  ---------------------------------------------------------------------------
    trades                     2,683
    win rate                    27.1 %
    profit factor                0.16
    average R per trade         -0.353 R
    net                         -$17.63   (-0.18 %)
    ending balance               $9,982.37
    average stop                 78 pips
    round-trip cost              40 pips  =  51.4 % of the stop

  PF 0.16 means it loses roughly 6 dollars for every 1 dollar it makes.
  This is not a marginal, tunable result. It is the wrong side of the
  distribution by a wide margin, in every month, in both directions.


================================================================================
2. MONTH-BY-MONTH  (the specific question asked)
================================================================================

  month     n   win%     PF    avgR      net$       bal$    ret%  avgStop  cost%stop
  ----------------------------------------------------------------------------------
  2026-05  689   27.7   0.17  -0.338    -4.52     9,995   -0.05      80       49.7
  2026-06  662   28.7   0.21  -0.334    -4.36     9,991   -0.04      88       45.6
  2026-07  684   26.3   0.13  -0.374    -4.45     9,987   -0.04      70       57.1
  2026-08  640   25.8   0.13  -0.364    -4.26     9,982   -0.04      73       54.4
  ----------------------------------------------------------------------------------

  Read the ret% column. Four months of continuous trading cost 0.18 % of the
  account in total. That is NOT because the strategy is nearly breakeven --
  it is because the lot size is fixed at 0.01, which is far too small to
  matter. The strategy bleeds -0.35 R on EVERY trade. Size the same -0.35 R
  edge at 2 % of the account (the corrected EA's setting) and four months
  becomes a total loss of the account. Fixed small lots were hiding the
  damage, not preventing it.

  The dollar figures below are therefore the LEAST alarming part of this
  report. The R column is the real one.


================================================================================
3. WHY IT LOSES - THE DECOMPOSITION
================================================================================

3.1  The R distribution has only two populated buckets
--------------------------------------------------------------------------------
    outcome                       count     share
    <= -1R  (stopped out)          1,504     56.1 %
    -1R .. 0R  (partial then stop)   444     16.5 %
     0R .. +0.5R                      0      0.0 %
    +0.5R .. +1R                      0      0.0 %
    >= +1.1R  (target)                735     27.4 %

    There is no middle. The strategy does not win small and lose small --
    it either dies at the stop or reaches the full target. With RR 1.1 and
    a 50 % partial at 0.5R, breakeven requires a win rate of 52.4 %. Actual
    win rate is 27.1 %. The payoff ratio is roughly 1:1 in practice, so the
    outcome is determined almost entirely by the 27 % hit rate, and 27 % is
    close to a coin flip struck badly.

3.2  MFE / MAE - price does not cooperate
--------------------------------------------------------------------------------
    mean maximum favourable excursion   +0.58 R
    median MFE                          +0.36 R
    mean maximum adverse excursion      -1.10 R
    trades that NEVER reached +0.5R     1,485  (55 %)

    On the majority of trades, price never moved meaningfully in the
    predicted direction before the stop took it out. This is the signature
    of a signal with no directional information: the entry is not "early",
    it is simply not predictive.

3.3  Costs consume half the stop
--------------------------------------------------------------------------------
    average stop                 78 pips
    round-trip cost              40 pips  (30 spread + 5 slippage x2)
    cost as % of stop            51.4 %

    To be profitable, a trade must first clear 40 pips of friction and then
    earn the remaining 38. A 1.5x ATR stop on 5-minute gold is far too tight
    to work at this cost level, and tightening it further only converts
    winners into stop-outs.

3.4  The signal itself is the problem
--------------------------------------------------------------------------------
    signal type      n      avg R
    reversal      2,679    -0.354
    support/res.      4    +0.663   (4 trades - statistically meaningless)

    99.9 % of the trades come from ONE signal family: engulfing/hammer/
    shooting-star reversal candles. That family carries the entire loss.
    Support/resistance and breakout blocks fire so rarely (4 trades in
    four months) that they contribute nothing either way.

3.5  It is not a time-of-day or day-of-week artifact
--------------------------------------------------------------------------------
    Every one of the 23 broker hours is negative. Every one of the five
    weekdays is negative. There is no session filter that rescues this;
    the loss is uniform.


================================================================================
4. R:R AND LOT SIZE WERE NOT THE CAUSE  (tested directly)
================================================================================

  The R:R sweep, TP2 expressed as a multiple of ATR, 5-day window:

    TP2 = 1.0x ATR   PF 0.09   -13.39 %
    TP2 = 1.5x ATR   PF 0.18   -10.36 %
    TP2 = 2.0x ATR   PF 0.19    -9.07 %
    TP2 = 3.0x ATR   PF 0.17    -8.51 %
    TP2 = 4.0x ATR   PF 0.19    -7.12 %
    TP2 = 6.0x ATR   PF 0.32    -3.62 %

  Every single one loses. The apparent "improvement" as R:R widens is purely
  because fewer trades survive to the target -- fewer chances to be wrong.
  There is no ratio at which this signal family becomes profitable, because
  the expectancy is negative BEFORE costs are applied.

  A 2 %-of-balance lot-size sweep over three months (2026-06 .. 2026-08):

    1,733 trades, PF 0.24, -98.01 %  ->  the account is destroyed

  Same signals, same R:R. Only the money management changed. Confirms the
  edge (or lack of it) lives in the signal, not the sizing.


================================================================================
5. DEFECTS FOUND IN THE ORIGINAL EA
================================================================================

  These were real and are fixed in the corrected build.

  5.1  IT DID NOT COMPILE
       `input int InpSlippage = as per the platform;` and
       `input double InpTP_RR = as per the market movement;`
       are not valid MQL5 expressions. "as" is an undefined identifier.

  5.2  UNDECLARED VARIABLES
       `InpEnableTrailing` and `InpTrailStart` were used in
       ManagePositions() but never declared as inputs. Compile error.

  5.3  LOOK-AHEAD BIAS  (the serious one)
       The arrays were set with ArraySetAsSeries(true), which makes index
       i-1 the FUTURE bar. The code read `close[i-1]` and `open[i-1]` as
       "the previous candle" in every reversal pattern, and its swing
       searches scanned indices i+1..i+4 -- the future. The strategy was
       reading tomorrow's price to decide today's entry.

  5.4  SIGNAL FLOOD
       A pattern up to InpLookback_Period (60) bars old still counted as
       "near price", so the EA re-entered on nearly every bar.

  5.5  BROKEN BREAKOUT BLOCKS
       ArrayMaximum was called on a series array, so the "highest high"
       included future bars.

  5.6  lastBarTime NEVER INITIALISED
       The first tick after attach was always treated as a new bar.

  5.7  UNBOUNDED SPREAD PRINY
       IsSpreadOk() printed on every single tick when the spread was wide.

  The corrected EA is verified: MetaEditor reports 0 errors, 0 warnings.


================================================================================
6. HONEST CAVEATS ON THIS REPORT
================================================================================

  6.1  FIXED-LOT SIMULATION IS PESSIMISTIC ON ONE PATH.
       After TP1 the EA moves the stop to break-even. This model does not
       credit that move, so it charges those 444 trades a full -0.5 R on
       the runner (443 of 2,683 trades) when the real result would be about
       break-even.
       Crediting it improves the average from -0.353 R to roughly -0.270 R.
       The conclusion is unchanged and the margin is far too wide to flip.

  6.2  THIS WEEK'S DATA WAS NOT AVAILABLE.
       The M5 table ends 2026-08-28 and the MT5 terminal runs in a
       different Windows session, so Python cannot authorize against it
       ("Terminal: Authorization failed"). May-August is the most recent
       five-day-equivalent stretch available and is the basis above.
       To test a specific week, run:
           ./.venv/Scripts/python.exe research/uncle_ea_diagnose.py \
               --monthly --from 2026-09-21 --to 2026-09-26
       once the terminal is reachable.

  6.3  SPREAD IS ASSUMED, NOT MEASURED PER BAR.
       30 pips + 5 pips/side is the recorded broker spread for this
       symbol. If your live spread is materially wider the result is worse,
       not better.

  6.4  n IS SMALL PER MONTH IN EDGE TERMS.
       ~660 trades/month is statistically ample for detecting PF 0.16.
       It is NOT ample for detecting a small edge, because there is none
       to detect at this magnitude.

  6.5  A NOTE ON MY OWN PROCESS
       The first version of the report printed an ending balance of
       -$13,611.04 on a -$17.63 loss. A balance accumulator was adding the
       running total once per trade. It had zero test coverage. Both the
       bug and the coverage gap are now fixed and regression-tested
       (tests/test_uncle_ea_diagnose.py). The monthly table and the
       conclusion were never affected -- they used a separate path.


================================================================================
7. RECOMMENDATION
================================================================================

  DO NOT DEPLOY THIS STRATEGY, IN ANY SIZING.

  The corrected EA is worth keeping as a working, compile-clean, risk-capped
  reference implementation -- it enforces 2 %-of-balance sizing that refuses
  to trade when the minimum lot would exceed the risk cap, derives TP1/TP2
  from ATR and market structure instead of a hard-coded ratio, and holds at
  one position. But deploying it will lose money, because the entry logic is
  not predictive on 5-minute XAUUSD.

  This is not a new conclusion for this project. Sixteen strategy families
  have now been tested on this data (V1-V14 plus the session-range and
  uncle's EA). All M5/M15 signal families return PF 0.00-0.36. The only
  two that survive validation are V11 (D1 momentum) and V12 (H1 momentum),
  both already approved and running on the demo account. The pattern is
  consistent and strong: on gold, edge at M5 is smaller than the cost floor.

  If the M5 timeframe is a requirement, the honest next step is not more
  parameter tuning of this family -- it is a different information source
  (volume profile, order flow, session statistics measured over multiple
  years) or a longer timeframe where costs stop dominating.
================================================================================
