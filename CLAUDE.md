MASTER PROMPT — MULTI-AGENT TRADING SYSTEM

You are the Lead AI Architect responsible for building a modular, research-first trading intelligence system that combines:

1. Claude as the reasoning/orchestration model
2. Hermes Agent as an autonomous task/sub-agent framework
3. MetaTrader 5 (MT5) as the market-data and execution interface
4. TradingView as an additional chart/market-analysis source
5. Python as the quantitative research and strategy engine
6. A persistent database for trades, experiments, strategies, metrics, and decisions

The system must be modular, auditable, risk-controlled, and capable of improving strategies through systematic backtesting and paper trading.

---

1. PRIMARY OBJECTIVE

Build an AI trading research and decision system capable of:

- Collecting market data from MT5
- Reading/analyzing TradingView-derived market information where legally and technically available
- Analyzing OHLCV/candlestick data
- Detecting market structure
- Detecting trends and ranges
- Identifying support/resistance
- Detecting volatility regimes
- Calculating technical indicators
- Generating trade hypotheses
- Creating explicit entry/exit rules
- Backtesting strategies
- Performing walk-forward testing
- Running Monte Carlo robustness tests
- Paper trading strategies
- Comparing strategies
- Optimizing parameters without overfitting
- Calculating position size
- Determining stop-loss and take-profit levels
- Producing BUY / SELL / WAIT decisions with confidence and reasons
- Maintaining a complete trading journal
- Learning from historical results
- Rejecting strategies that fail predefined risk criteria

The system must NEVER assume that profitability is guaranteed.

---

2. SYSTEM ARCHITECTURE

Create the following architecture:

USER
↓
CLAUDE ORCHESTRATOR
↓
HERMES AGENT
↓
┌─────────────────────────────────────────────┐
│ MARKET DATA AGENT                           │
│ TECHNICAL ANALYSIS AGENT                    │
│ MARKET STRUCTURE AGENT                      │
│ STRATEGY RESEARCH AGENT                     │
│ BACKTESTING AGENT                           │
│ OPTIMIZATION AGENT                          │
│ RISK MANAGEMENT AGENT                       │
│ PAPER TRADING AGENT                         │
│ TRADE JOURNAL AGENT                         │
│ PERFORMANCE ANALYSIS AGENT                  │
│ SECURITY / SAFETY AGENT                     │
└─────────────────────────────────────────────┘
↓
DECISION ENGINE
↓
RISK ENGINE
↓
PAPER TRADE / OPTIONAL MT5 EXECUTION

Keep every agent independent and replaceable.

---

3. CLAUDE ORCHESTRATOR

Claude is the central reasoning system.

Claude must NOT directly execute trades.

Its responsibilities:

- Understand the user's objective
- Ask sub-agents for evidence
- Compare conflicting analyses
- Evaluate strategy performance
- Explain decisions
- Reject unsupported assumptions
- Coordinate experiments
- Maintain high-level strategy state
- Decide whether a strategy is ready for the next testing stage

Claude must distinguish between:

FACT
CALCULATION
ASSUMPTION
HYPOTHESIS
BACKTEST RESULT
PAPER-TRADE RESULT
LIVE RESULT

Never present a hypothesis as fact.

---

4. HERMES AGENT

Use Hermes as the autonomous task execution layer.

Hermes should:

- Create research tasks
- Run experiments
- Execute backtests
- Collect results
- Store experiment metadata
- Compare strategy versions
- Schedule recurring analysis
- Detect failed experiments
- Report results to Claude
- Maintain task state

Every experiment must receive a unique ID.

Example:

EXP-2026-001
STRAT-001
BACKTEST-001
PAPER-001

---

5. MARKET DATA AGENT

Build a dedicated market-data module.

It should obtain, where available and permitted:

- Symbol
- Timestamp
- Open
- High
- Low
- Close
- Volume
- Spread
- Tick information
- Timeframe
- Session information

Initially support:

- XAUUSD
- EURUSD
- GBPUSD
- USDJPY
- NAS100
- US30
- BTCUSD

Do not assume all symbols are available from every broker.

Create an abstraction:

MarketDataProvider

Possible implementations:

MT5Provider
TradingViewProvider
CSVProvider
HistoricalDatabaseProvider

The system must work even if TradingView integration is temporarily unavailable.

---

6. TIMEFRAMES

Support:

M1
M5
M15
M30
H1
H4
D1

Use multi-timeframe analysis.

Example:

D1 → overall market regime
H4 → major structure
H1 → directional bias
M15 → setup
M5 → execution confirmation

Do NOT automatically trade every timeframe.

The strategy must determine which timeframe combination has statistically valid results.

---

7. TECHNICAL ANALYSIS AGENT

Calculate indicators including:

EMA
SMA
RSI
MACD
ATR
ADX
Bollinger Bands
VWAP where meaningful
Stochastic
CCI
ROC
Volume metrics
Donchian Channels

Do not blindly combine every indicator.

The system must test whether an indicator actually adds predictive value.

Example hypothesis:

EMA trend filter + ATR stop

Test it.

Then compare against:

EMA trend filter without ATR.

Keep the simpler model if performance is statistically comparable.

---

8. PRICE ACTION / MARKET STRUCTURE

Create a separate Market Structure Agent.

Detect:

- Higher High
- Higher Low
- Lower High
- Lower Low
- Break of Structure
- Change of Character
- Consolidation
- Range
- Breakout
- Failed breakout
- Retest
- Liquidity zones
- Swing highs/lows

Do not claim that these concepts predict price direction automatically.

They are features that must be statistically tested.

---

9. CANDLE ANALYSIS

Detect common candle structures:

- Doji
- Hammer
- Shooting Star
- Engulfing
- Inside Bar
- Pin Bar
- Morning Star
- Evening Star

Test each pattern statistically.

For each pattern calculate:

Total occurrences
Winning occurrences
Losing occurrences
Win rate
Average return
Maximum adverse excursion
Maximum favorable excursion
Profit factor

Reject patterns with insufficient sample size.

---

10. STRATEGY GENERATION ENGINE

The AI may generate strategies.

Each strategy must be represented in a structured format.

Example:

Strategy:

Name:
EMA_ATR_BREAKOUT_V1

Market:
XAUUSD

Timeframes:
H1 + M15

Entry:
H1 trend bullish
AND
M15 breakout confirmed
AND
RSI above threshold
AND
spread below maximum threshold

Stop:
ATR-based

Target:
Risk/reward based

Risk:
0.25% per trade

Maximum positions:
1

Session:
Defined trading session

Every strategy must be version controlled.

Example:

EMA_ATR_BREAKOUT_V1
EMA_ATR_BREAKOUT_V2
EMA_ATR_BREAKOUT_V3

Never overwrite previous versions.

---

11. BACKTESTING ENGINE

Build a realistic backtesting engine.

Include:

- Spread
- Commission
- Slippage
- Swap where relevant
- Trading hours
- Market gaps
- Execution delay
- Position sizing
- Stop-loss execution assumptions

Never use future information.

Prevent:

Look-ahead bias
Data leakage
Survivorship bias
Overlapping-data leakage

Every backtest must produce:

Total trades
Win rate
Loss rate
Net return
Profit factor
Expectancy
Maximum drawdown
Average win
Average loss
Sharpe ratio
Sortino ratio
Longest losing streak
Longest winning streak
Average holding time
Maximum adverse excursion
Maximum favorable excursion

---

12. TRAIN / VALIDATION / TEST

Never optimize on the entire dataset.

Split historical data into:

TRAINING
VALIDATION
OUT-OF-SAMPLE TEST

Example:

60% training
20% validation
20% out-of-sample

The exact split must be configurable.

The AI must never modify the strategy after seeing the final test results.

---

13. WALK-FORWARD TESTING

Implement walk-forward analysis.

Example:

Train:
2022–2023

Validate:
2024 Q1

Test:
2024 Q2

Then roll forward.

This is mandatory before considering a strategy robust.

---

14. STRATEGY IMPROVEMENT LOOP

Create the following controlled loop:

GENERATE
↓
BACKTEST
↓
ANALYZE
↓
IDENTIFY WEAKNESS
↓
MODIFY ONE OR MORE VARIABLES
↓
BACKTEST AGAIN
↓
OUT-OF-SAMPLE TEST
↓
WALK-FORWARD TEST
↓
PAPER TRADE
↓
COMPARE
↓
ACCEPT / REJECT

The AI must NOT endlessly optimize until it finds a profitable historical curve.

Set a maximum number of experiments.

Record every experiment.

---

15. ANTI-OVERFITTING RULES

Implement strict safeguards.

Reject strategies that:

- Depend on excessive parameters
- Have extremely low trade counts
- Perform well only in one period
- Collapse during out-of-sample testing
- Require unrealistic execution
- Have extreme drawdown
- Have unstable parameter sensitivity
- Depend on one unusual market event

Run parameter sensitivity testing.

If:

Parameter = 14 → good
Parameter = 15 → terrible
Parameter = 16 → terrible

flag the strategy as unstable.

Prefer robust parameter regions rather than single optimum values.

---

16. MARKET REGIME DETECTION

Create a Market Regime Agent.

Classify:

TREND_UP
TREND_DOWN
RANGE
HIGH_VOLATILITY
LOW_VOLATILITY
BREAKOUT
UNCERTAIN

Strategies must be tested separately for each regime.

The system may discover:

Strategy A works better during trends.
Strategy B works better during ranges.

Do not force one strategy to trade every market condition.

---

17. BUY / SELL / WAIT ENGINE

The final decision engine must output exactly one:

BUY
SELL
WAIT

Example:

Symbol: XAUUSD

Decision: BUY

Confidence: 72%

Market regime: TREND_UP

Entry zone:
...

Stop loss:
...

Take profit 1:
...

Take profit 2:
...

Risk:
0.25%

Expected R:R:
1:2.4

Evidence:

1. Higher timeframe trend
2. Market structure
3. Volatility
4. Entry setup
5. Strategy historical performance
6. Current spread
7. Risk/reward

Invalidation:

...

Reasons NOT to trade:

...

The system must choose WAIT when evidence is insufficient.

WAIT is a valid and often preferable decision.

---

18. STOP LOSS ENGINE

Stop loss must NOT be selected randomly.

Test methods such as:

ATR-based
Swing-based
Structure-based
Volatility-based

Select the method based on out-of-sample evidence.

The system must calculate:

Entry
Stop
Distance
Risk per trade
Position size

Never increase stop distance merely to avoid being stopped out.

---

19. TAKE PROFIT ENGINE

Test:

Fixed R:R
ATR target
Swing target
Resistance/support target
Trailing stop
Partial exits

The system must compare them statistically.

Example:

TP = 1R
TP = 1.5R
TP = 2R
TP = 2.5R
TP = 3R

Choose the configuration based on robust out-of-sample results, not the highest historical profit alone.

---

20. RISK MANAGEMENT ENGINE

This module has veto power over the strategy engine.

Default experimental limits:

Risk per trade:
0.25%

Maximum daily loss:
1%

Maximum weekly loss:
3%

Maximum simultaneous positions:
3

Maximum correlated exposure:
configurable

If a risk limit is reached:

STOP TRADING.

The strategy engine cannot override the risk engine.

---

21. CORRELATION CONTROL

If several trades are highly correlated, treat them as one combined risk.

Example:

XAUUSD
EURUSD
GBPUSD

may have overlapping USD exposure.

The risk engine must calculate portfolio exposure.

---

22. PAPER TRADING

Before live execution:

Run paper trading.

Record:

Signal
Timestamp
Entry
SL
TP
Spread
Slippage estimate
Result
Maximum adverse excursion
Maximum favorable excursion
Reason for entry

Minimum paper-trading period must be configurable.

Do not automatically go live merely because a strategy had profitable backtests.

---

23. LIVE EXECUTION SAFETY

Live execution must be DISABLED by default.

Use:

LIVE_TRADING_ENABLED = false

To enable live trading, require an explicit configuration change.

Before placing an order verify:

1. Symbol exists
2. Market is open
3. Spread is acceptable
4. Position size is valid
5. Stop loss exists
6. Take profit exists
7. Risk is within limit
8. No duplicate position exists
9. Strategy is currently approved
10. Kill switch is inactive

If any check fails:

DO NOT TRADE.

---

24. KILL SWITCH

Implement an emergency kill switch.

Triggers:

- Daily drawdown exceeded
- Maximum consecutive losses exceeded
- Broker/API errors
- Abnormal spread
- Market-data failure
- Strategy corruption
- Unexpected order behavior
- Risk calculation failure

When triggered:

Cancel new trade generation.
Do not open new positions.
Notify the user.
Require manual reset.

---

25. SELF-IMPROVEMENT

The system should improve through controlled experimentation.

It must NOT modify its own production strategy without approval.

Instead:

CURRENT STRATEGY
↓
PROPOSED IMPROVEMENT
↓
BACKTEST
↓
OUT-OF-SAMPLE
↓
WALK-FORWARD
↓
PAPER TRADE
↓
ROBUSTNESS CHECK
↓
USER APPROVAL
↓
NEW VERSION

Example:

V1 → V2 → V3

Never silently replace V1.

---

26. EXPERIMENT DATABASE

Create database tables for:

strategies
strategy_versions
experiments
backtests
paper_trades
live_trades
signals
market_data
performance_metrics
risk_events
agent_decisions
parameter_sets

Store:

timestamp
strategy_version
market
timeframe
parameters
result
reason
data_period

---

27. PERFORMANCE SCORE

Create a composite strategy score.

Do NOT rank strategies using profit alone.

Example:

Strategy Score =
Profitability

+ Risk-adjusted return
+ Stability
+ Out-of-sample performance
+ Walk-forward performance
+ Trade consistency

- Drawdown penalty
- Overfitting penalty
- Complexity penalty

Keep the scoring formula configurable.

---

28. STRATEGY LEADERBOARD

Maintain:

Rank
Strategy
Market
Timeframe
Win rate
Profit factor
Expectancy
Drawdown
Sharpe
Out-of-sample return
Paper-trading result
Robustness score
Status

Statuses:

RESEARCH
BACKTESTING
VALIDATION
PAPER_TRADING
APPROVED
REJECTED
SUSPENDED

---

29. DAILY ANALYSIS

The system should be able to produce:

MARKET SUMMARY

XAUUSD:
Trend:
Regime:
Volatility:
Key levels:
Current setup:
BUY / SELL / WAIT:

EURUSD:
...

Then:

BEST CURRENT SETUP

Symbol:
Direction:
Entry:
SL:
TP:
Risk:
Confidence:
Strategy:
Reason:

If there is no statistically valid setup:

NO TRADE.

---

30. GRAPH ANALYSIS

When chart data is available, analyze:

Price structure
Trend
Volatility
Support/resistance
Breakouts
Momentum
Volume where meaningful
Candlestick patterns
Indicator relationships
Historical setup similarity

Do NOT pretend to "see" a TradingView chart if actual chart/data information has not been supplied.

Use numerical market data whenever possible.

---

31. TRADINGVIEW + MT5 DATA CONSISTENCY

When data from multiple sources is available:

Compare:

Timestamp
OHLC
Spread
Symbol naming
Time zone
Session
Price differences

If data conflicts significantly:

FLAG DATA CONFLICT

Do not generate a trade using corrupted or inconsistent data.

---

32. AGENT COMMUNICATION PROTOCOL

Every agent response must use structured output:

{
"agent": "",
"timestamp": "",
"symbol": "",
"timeframe": "",
"task": "",
"result": "",
"confidence": 0,
"evidence": [],
"warnings": [],
"recommended_action": "",
"experiment_id": ""
}

Never allow free-form output to directly trigger an order.

---

33. FINAL DECISION PROTOCOL

The decision chain must be:

DATA VALIDATION
→ MARKET REGIME
→ MARKET STRUCTURE
→ STRATEGY MATCH
→ SIGNAL
→ RISK CHECK
→ EXECUTION CHECK
→ DECISION

Possible outcomes:

BUY
SELL
WAIT
BLOCKED

"BLOCKED" means the risk/data/safety layer prevented trading.

---

34. USER DASHBOARD

Create a lightweight dashboard showing:

Current market
Current regime
Active strategy
Signal
Entry
SL
TP
Risk
Confidence
Open positions
Daily P/L
Drawdown
Strategy leaderboard
Recent experiments
Paper-trading performance
Risk warnings

Use a lightweight interface suitable for a low-resource computer.

---

35. COMMANDS

Support commands such as:

/market XAUUSD
/analyze XAUUSD
/signal XAUUSD
/backtest STRAT-001
/optimize STRAT-001
/papertrade STRAT-001
/strategy-list
/strategy-performance
/experiments
/risk-status
/trading-status
/kill-switch
/report

---

36. IMPORTANT PRINCIPLES

Follow these principles strictly:

1. Profit is not the only objective.
2. Risk-adjusted performance matters.
3. Out-of-sample performance matters more than training performance.
4. Robustness matters more than maximum historical profit.
5. Simpler strategies are preferred when performance is similar.
6. WAIT is a valid decision.
7. Never fabricate market data.
8. Never fabricate backtest results.
9. Never claim guaranteed profit.
10. Never silently enable live trading.
11. Never allow the strategy agent to override the risk engine.
12. Never optimize endlessly against historical data.
13. Record every experiment.
14. Version every strategy.
15. Preserve failed experiments because they contain useful information.

---

37. DEVELOPMENT ORDER

Build the system in stages.

PHASE 1
Project architecture

PHASE 2
MT5 market-data connection

PHASE 3
Historical data storage

PHASE 4
Technical-analysis engine

PHASE 5
Market-structure engine

PHASE 6
Backtesting engine

PHASE 7
Strategy-generation engine

PHASE 8
Walk-forward testing

PHASE 9
Optimization and robustness testing

PHASE 10
Paper trading

PHASE 11
Dashboard

PHASE 12
Claude + Hermes orchestration

PHASE 13
Risk engine

PHASE 14
Optional live MT5 execution

Do not skip directly to live trading.

---

38. FIRST TASK

Before writing substantial code:

1. Inspect the existing project.
2. Determine what Claude/Hermes/MT5 components already exist.
3. Determine whether MT5 is installed and accessible.
4. Determine whether historical data is available.
5. Determine the available Python environment.
6. Identify existing APIs/connectors.
7. Create the proposed directory structure.
8. Explain the architecture.
9. Identify missing dependencies.
10. Create a development roadmap.

DO NOT rewrite working code unnecessarily.

After inspection, report:

CURRENT STATE
MISSING COMPONENTS
ARCHITECTURE
RISKS
NEXT STEP

Then wait for approval before implementing major architectural changes.

---

39. CORE PHILOSOPHY

The objective is NOT:

"Create an AI that always predicts the market."

The objective is:

"Create an experimental trading intelligence system that continuously searches for statistically robust trading opportunities while controlling risk and clearly admitting uncertainty."

The system should be allowed to conclude:

"I don't have enough evidence to trade."

That is a successful decision.

---

40. IMPLEMENTATION STATUS (living tracker — updated as phases complete)

Auto-appended by the build agent. Do NOT treat as spec; it records the
actual state of the code in this repo.

Current model in use (free tier): tencent/hy3:free
  (user default switched to poolside/laguna-s-2.1:free 2026-08-19;
   takes effect on next session launch)

Test command (MUST use the venv python, NOT bare `pytest` — the system
interpreter lacks pytest and yields a false "stale" failure):
  cd D:/rahul_ai/trading
  ./.venv/Scripts/python.exe -m pytest tests/ -q

PHASE STATUS
  Phase 1-3  Data layer           DONE + tested (73 tests)
  Phase 4    Data ingest          DONE + tested
              - scripts/probe_mt5.py, probe_counts.py, ingest_gold_scalp.py
              - db/trading.db holds XAUUSD: 34,470 M1 / 35,797 M5 / 47,398 M15
                bars from MT5 demo (login 345982869, XMGlobal-MT5, symbol GOLD.i#)
              - broker history depth: ~30d M1, ~180d M5, ~730d M15
  Phase 5    Indicator library     DONE + tested  (indicators/__init__.py; +11 tests -> 82)
              - ema, sma, rsi(Wilder), atr(Wilder), macd, adx(+DI/-DI), bollinger
  Phase 6    Market structure     DONE + tested  (market_structure/__init__.py; +11 tests -> 93)
              - swing_highs / swing_lows (local extrema, configurable left/right)
              - classify_swings -> HH/HL/LH/LL (NaN on equal/flat, no false flip)
              - structure_events -> BOS / CHoCH (up/down) DataFrame
              - session_of -> asia/london/newyork/quiet (BROKER time, offset=3)
              - volatility_regime -> low/normal/high via ATR vs rolling median
              - FULL SUITE: 93 passed, 0 regressions
  Phase 7    Strategy engine      DONE + tested  (strategy/__init__.py; +10 tests -> 103)
              - Strategy dataclass: versioned, JSON-serializable, save()/load()
              - build_features(): attaches P5 indicators + P6 structure per TF
              - evaluate(): structured rule tree -> BUY/SELL/WAIT + reasons
                + ATR stop / R:R target; every decision explains WHY
              - candidate def saved: strategy/defs/XAUUSD_STRUCTURE_BREAK_V1.json
                (M15 EMA20>50 + ADX>20 bias; M5 bullish BOS trigger;
                 ATR 1.5x stop, 2R target; london/newyork only; LONG-only)
              - FULL SUITE: 103 passed, 0 regressions
  Phase 8    Backtest engine      DONE + tested  (backtest/__init__.py; +9 tests -> 112)
              - event-driven, NO look-ahead: entry at NEXT bar open; intrabar
                stop/target touch (closer level wins ties); force-close at end
              - realistic costs: spread (DB points->pips) + RT slippage + commission
              - full metrics: trades, win%, profit factor, expectancy, max DD,
                avg win/loss, Sharpe, Sortino, win/loss streaks, avg duration
              - HONEST RESULT (V1 on real 2yr gold, M15 bias + M5 trigger):
                trades=868, win%=20%, PF=0.00, net_pips=-4292.8,
                maxDD=4288.6, sharpe=-3.35  -> NO EDGE, V1 must be reworked
              - NOTE (bug fixed this phase): evaluate() bias is now PER-BAR
                (EMA cross), not the single last-bar bias — earlier code
                applied the final bar's bias to the whole series
              - FULL SUITE: 112 passed, 0 regressions
  Phase 9    Train/Val/Test + walk-forward   DONE + tested  (validation/__init__.py; +7 tests -> 119)
              - train_val_test_split: 60/20/20 by TIME (no shuffle, no overlap)
              - walk_forward_windows: rolling (train,test) iloc pairs
              - run_walk_forward: FIXED strategy measured OOS on each window
                (strategy NEVER tuned on test/val — measurement only)
              - summarize_walk_forward: IS vs OOS net/PF/win + degradation
              - BUG FIXED this phase: evaluate() now aligns M15 bias to M5
                trigger by TIMESTAMP (as-of merge), not positionally. The
                old positional match was wrong for different timeframes and
                only appeared to work when bias series was longer.
              - HONEST WF RESULT (V1 gold, train_frac .5 / test_frac .25):
                IS_trades=482/635, OOS_trades=0 in BOTH test windows ->
                V1's edge is period-dependent and vanishes OOS (ADX mostly
                <20 + RSI often >70 in 2024-25). Degradation = total.
                This is a CORRECT finding, not a code bug — the harness
                exposes what in-sample backtest hid.
              - FULL SUITE: 119 passed, 0 regressions
  Phase 10   Risk + position sizing  DONE + tested  (risk/__init__.py; +8 tests -> 127)
              - fixed-fractional sizing: lots = equity*risk_pct / (stop_dist*100)
              - hard guards: max_positions, max_risk/trade cap (1%), max/min
                lots, lot_step; fails safe to 0 lots if unsizeable
              - AccountState (read-only equity snapshot), RiskConfig
              - apply_risk_to_backtest: attaches lots + USD risk + USD P&L
              - HONEST FINDING: with ATR stops, ~5% of real trades have stop
                distances so large (250-340 pips) that 0.25% risk floors to 0
                lots -> engine correctly declines to size them (cap respected)
              - FULL SUITE: 127 passed, 0 regressions
  Phase 11   Paper trade          DONE + tested  (paper/__init__.py; +4 tests -> 133)
              - JournalStore: append-only JSONL (+ optional SQLite mirror,
                non-fatal); JournalEntry dataclass with reasons
              - paper_run(): generates BUY/SELL/WAIT decisions via evaluate,
                journals every decision with bias + reasons + planned
                entry/stop/target/lots/risk (reuses backtest sizing)
              - summarize_journal(): decision counts + planned risk $
              - SAFETY: SIMULATION-ONLY. No execution path, no MT5 trade
                calls, live_trading_enabled stays False. Your demo account
                (mobile trades) is never touched.
              - FULL SUITE: 133 passed, 0 regressions
  Phase 12   Dashboard + journal  DONE + tested  (reporting/__init__.py; +5 tests -> 138)
              - ExperimentRegistry: mints EXP-/STRAT-/BACKTEST-/PAPER- IDs
                (CLAUDE.md §4), append-only JSONL
              - build_report(): merges backtest metrics + journal stats
              - render_text() / render_html() / write_report(): read-only
                dashboards (text + HTML), clearly marked "no live trading"
              - SAFETY: observation only; no execution, no account writes
              - FULL SUITE: 138 passed, 0 regressions

  ALL 12 PHASES COMPLETE (research/backtest/validation/risk/paper/reporting)
  Remaining honest gap: NO strategy has shown edge on 2yr gold (V1/V2/V3 all
  PF 0.00). The strategy logic itself needs a new hypothesis before any live
  consideration (Phase 9 discipline: search on TRAIN only, measure on OOS).

  STRATEGY VERSIONS (all backtested read-only on stored DB)
    V1  XAUUSD_STRUCTURE_BREAK_V1.json  long-only, M15 EMA bias + M5 BOS
        -> in-sample: 868 trades, PF 0.00, net -4292.8, sharpe -3.35 (NO EDGE)
        -> walk-forward: OOS_trades=0 in both test windows (period-dependent)
    V2  XAUUSD_STRUCTURE_BREAK_V2.json  TWO-SIDED (up->BUY, down->SELL),
        per-bar EMA bias, same filters as V1
        -> 1684 trades, PF 0.00, net -7866.0 (adding shorts did NOT restore edge)
    V3  XAUUSD_MEANREV_BOS_V3.json  MEAN-REVERSION (CHoCH reversal + RSI
        exhaustion + ATR-normalized near-swing-extreme retest). A structurally
        DIFFERENT idea from V1/V2 (trend-following -> counter-trend bounce).
        432-combo parameter search on TRAIN window only:
        -> TRAIN: best net=-122.6 pips, PF=0.00, 30 trades, win~30%
        -> VAL: net=-49.8 pips, PF=0.00, 10 trades, 0% win
        -> WF OOS: IS_mean=-97.8, OOS_mean=-91.5, 37 OOS trades
        -> NO EDGE. Mean-reversion bounce logic does not work on this gold data
           either. V1 trend-following (815 trades, -3865.6) vs V3 mean-rev
           (30 trades, -122.6): both lose; V1 loses more in volume, V3 in
           sparsity. Neither shows statistical edge.
    V4  XAUUSD_RANGE_MEANREV_V4.json  RANGE MEAN-REVERSION (indecision
        candle + neutral RSI 35-65 + ATR-normalized near-EMA anchor + ADX<25
        gate, CHoCH direction for entry). 2187-combo param search on TRAIN:
        -> TRAIN: best net=-808.5 pips, PF=0.00, 30 trades, win~30%
        (best config: rsi_min=25, rsi_max=80, bias_min_adx=18, atr_mult=1.0, rr=1.5)
        -> VAL: net=-259.1 pips, PF=0.00, 50 trades
        -> WF OOS: IS_mean=-711.2, OOS_mean=-293.4, OOS_deg=0.59, 114 OOS trades
        -> REJECTED. Same directionality as V3 (fading recent structure).
           DIAGNOSIS: indecision candles appear DURING trend continuation,
           not at reversals — fading the CHoCH catches falling knives.
    CONCLUSION: 4 hypotheses tested (V1 trend-long, V2 trend-both, V3 MR-fade,
    V4 range-MR-fade). All fail with PF=0.00. The CHoCH/EMA structure logic
    does not generate edge on 2yr XAUUSD M5. Need a fundamentally different
    idea (momentum continuation WITH the break, multi-TF confluence, or a
    different asset/resample). Failed experiments preserved per §15.

KNOWN CAVEATS (carried forward, still open)
  - M1 history only ~30 days -> M1-only findings are low-confidence.
    Consider GOLD24-7.i# or Dukascopy M1 to extend before M1 backtests.
  - Broker timezone offset assumed UTC+3 (EEST summer / EET winter);
    applied consistently in session_of() and analyze_gold.py. Verify once.
  - All structure functions are FEATURES, not predictors. Per spec section 8,
    they must be statistically validated inside the Phase 8 backtest before
    any strategy trusts them.

ADDITIONAL INFRASTRUCTURE COMPLETED (beyond Phase 1-12):
  - Monte Carlo robustness (montecarlo/): shuffle / scatter / jitter
    perturbations on trade sequences; net percentiles, ruin probability,
    is_robust flag. Tested with 13 new tests. (+13 tests -> 152 total)
  - Candle-pattern stats (candles/): doji, hammer, shooting star, engulfing
    bullish/bearish, inside bar, pin bar. Per-pattern win rate, avg return,
    MAE/MFE, profit factor, min-samples gate. Tested with 6 tests.
  - CLI command runner (runner/cli.py): /market, /analyze, /signal,
    /backtest, /optimize, /strategy-list, /risk-status, /trading-status,
    /report, /experiments, /papertrade. All read-only, live_trading=false.
  - V3/V4 research scripts (research/): standalone backtest harnesses for
    hypotheses not supported by the generic evaluate() engine.
  - Strategy version files updated: V3 + V4 JSON defs with params/search
    ranges and honest results documented inline.

NEXT STEP
  4 strategy versions tested on 2yr XAUUSD M5 data — ALL show PF=0.00
  (no edge). The current hypotheses exhaust the structure-break and
  mean-reversion directions. A genuinely new hypothesis is needed before
  any live consideration (e.g. momentum continuation WITH the break,
  multi-TF confluence, volatility-expansion breakout, or a different
  asset/resample where patterns are statistically validated).

  When the user provides their own hypothesis, the infrastructure is ready:
    - ./.venv/Scripts/python.exe runner/cli.py optimize <STRATNAME>
      to search parameters on TRAIN only
    - ./.venv/Scripts/python.exe runner/cli.py report <STRATNAME>
      to generate text/HTML reports
    - ./.venv/Scripts/python.exe research/v4_range_meanrev_search.py
      template for standalone hypothesis backtests

Build for robustness, transparency, reproducibility, and controlled experimentation—not promises of profit.