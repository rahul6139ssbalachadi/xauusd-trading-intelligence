# V11 monthly backtest — BTCUSD (D1)

- run_id: `MBT-20260926062702-3b504b`
- logic: execution/run_v11_daily.py:PARAMS + research/v11_d1_momentum.py:compute_d1_signals
- commit: `fc44c75` at 2026-09-26T06:27:02.797429+00:00
- data: 2026-01-01 03:00:00+00:00 .. 2026-09-25 03:00:00+00:00 (268 D1 bars)
- timezone: broker server time = UTC+3 (assumption: summer EEST applied year-round)
- granularity: OHLC bars; tick_volume and per-bar spread (points) recorded. No tick-level replay available.
- start balance: $100,000.00, risk 1.0%/trade, max 1 position
- costs: measured spread, 1.0 pip/side slippage, commission $0.0/lot, swap $0 (NOT modelled)
- safety: order_capable=False, live_trading_enabled=True (reported, never modified)

| period | signals | buy_signals | sell_signals | trades | wins | losses | win_rate | net_pnl | gross_profit | gross_loss | profit_factor | max_drawdown | avg_r | r_multiple | expectancy | avg_trade | largest_winner | largest_loser | consec_wins | consec_losses | sl_exits | tp_exits | other_exits | start_balance | end_balance | return_pct | total_costs | skipped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-02 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-03 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-04 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-05 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-06 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-07 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 100,000.00 | 100,000.00 | 0.00% | n/a | 0 |
| 2026-08 | 2 | 2 | 0 | 1 | 1 | 0 | 100.0% | 1,949.82 | 517,672.87 | n/a | n/a | 0.00 | 1.98 | 1.98 | 5,131.09 | 1,949.82 | 1,949.82 | 1,949.82 | 1 | 0 | 0 | 1 | 0 | 100,000.00 | 101,949.82 | 1.95% | 17.34 | 1 |
| 2026-09 | 3 | 3 | 0 | 2 | 1 | 1 | 50.0% | -268.55 | 273,080.00 | 363,758.15 | 0.73 | 0.00 | -0.13 | -0.13 | -497.32 | -134.28 | 725.45 | -994.01 | 1 | 1 | 1 | 0 | 1 | 100,000.00 | 99,731.45 | -0.27% | 23.72 | 1 |

Simulated research only. No orders were placed.
