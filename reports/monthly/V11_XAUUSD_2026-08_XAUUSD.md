# V11 monthly backtest — XAUUSD (D1)

- run_id: `not saved`
- logic: execution/run_v11_daily.py:PARAMS + research/v11_d1_momentum.py:compute_d1_signals
- commit: `fc44c75` at 2026-09-26T10:35:49.553737+00:00
- data: 2016-09-02 03:00:00+00:00 .. 2026-08-28 03:00:00+00:00 (2577 D1 bars)
- timezone: broker server time = UTC+3 (assumption: summer EEST applied year-round)
- granularity: OHLC bars; tick_volume and per-bar spread (points) recorded. No tick-level replay available.
- start balance: $10,000.00, risk 1.0%/trade, max 1 position
- costs: measured spread, 1.0 pip/side slippage, commission $0.0/lot, swap $0 (NOT modelled)
- safety: order_capable=False, live_trading_enabled=False (reported, never modified)

| period | signals | buy_signals | sell_signals | trades | wins | losses | win_rate | net_pnl | gross_profit | gross_loss | profit_factor | max_drawdown | avg_r | r_multiple | expectancy | avg_trade | largest_winner | largest_loser | consec_wins | consec_losses | sl_exits | tp_exits | other_exits | start_balance | end_balance | return_pct | total_costs | skipped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-08 | 1 | 1 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 10,000.00 | 10,000.00 | 0.00% | n/a | 1 |

Simulated research only. No orders were placed.
