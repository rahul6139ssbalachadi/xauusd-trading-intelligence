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

Build for robustness, transparency, reproducibility, and controlled experimentation—not promises of profit.