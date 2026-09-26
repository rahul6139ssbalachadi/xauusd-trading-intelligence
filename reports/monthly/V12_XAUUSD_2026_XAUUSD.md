# V12 monthly backtest — XAUUSD (H1)

- run_id: `MBT-20260926065052-773ef5`
- logic: execution/run_v12_hourly.py:PARAMS + execution/run_v12_hourly.py:signal_on_last_closed_bar gate set
- commit: `fc44c75` at 2026-09-26T06:50:52.034288+00:00
- data: 2016-09-01 12:00:00+00:00 .. 2026-08-29 02:00:00+00:00 (59313 H1 bars)
- timezone: broker server time = UTC+3 (assumption: summer EEST applied year-round)
- granularity: OHLC bars; tick_volume and per-bar spread (points) recorded. No tick-level replay available.
- start balance: $100,000.00, risk 2.0%/trade, max 1 position
- costs: measured spread, 1.0 pip/side slippage, commission $0.0/lot, swap $0 (NOT modelled)
- safety: order_capable=False, live_trading_enabled=True (reported, never modified)

| period | signals | buy_signals | sell_signals | trades | wins | losses | win_rate | net_pnl | gross_profit | gross_loss | profit_factor | max_drawdown | avg_r | r_multiple | expectancy | avg_trade | largest_winner | largest_loser | consec_wins | consec_losses | sl_exits | tp_exits | other_exits | start_balance | end_balance | return_pct | total_costs | skipped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01 | 10 | 10 | 0 | 8 | 3 | 5 | 37.5% | 3,836.55 | 2,254,403.71 | 1,640,806.37 | 1.36 | 1,011.58 | 0.49 | 0.49 | 74.46 | 479.57 | 2,999.54 | -1,040.23 | 3 | 3 | 5 | 3 | 0 | 100,000.00 | 103,836.55 | 3.84% | 69.00 | 2 |
| 2026-02 | 4 | 4 | 0 | 4 | 2 | 2 | 50.0% | 2,476.39 | 898,100.72 | 404,501.57 | 2.18 | 210.80 | 0.64 | 0.64 | 121.15 | 619.10 | 2,919.78 | -1,011.82 | 1 | 1 | 2 | 1 | 1 | 100,000.00 | 102,476.39 | 2.48% | 44.91 | 0 |
| 2026-03 | 4 | 4 | 0 | 4 | 0 | 4 | 0.0% | -3,916.55 | n/a | 1,169,308.09 | 0.00 | 956.62 | -1.01 | -1.01 | -294.63 | -979.14 | -964.83 | -998.63 | 0 | 4 | 4 | 0 | 0 | 100,000.00 | 96,083.45 | -3.92% | 32.30 | 0 |
| 2026-04 | 7 | 7 | 0 | 6 | 3 | 3 | 50.0% | 5,184.58 | 1,789,453.15 | 663,793.55 | 2.66 | 379.22 | 0.87 | 0.87 | 185.29 | 864.10 | 3,072.82 | -1,087.87 | 2 | 2 | 3 | 2 | 1 | 100,000.00 | 105,184.58 | 5.18% | 68.71 | 1 |
| 2026-05 | 1 | 1 | 0 | 1 | 0 | 1 | 0.0% | -1,004.69 | n/a | 154,630.71 | 0.00 | 0.00 | -1.02 | -1.02 | -156.98 | -1,004.69 | -1,004.69 | -1,004.69 | 0 | 1 | 1 | 0 | 0 | 100,000.00 | 98,995.31 | -1.00% | 15.05 | 0 |
| 2026-06 | 1 | 1 | 0 | 1 | 1 | 0 | 100.0% | 2,958.04 | 645,453.52 | n/a | n/a | 0.00 | 2.99 | 2.99 | 643.05 | 2,958.04 | 2,958.04 | 2,958.04 | 1 | 0 | 0 | 1 | 0 | 100,000.00 | 102,958.04 | 2.96% | 11.05 | 0 |
| 2026-07 | 4 | 4 | 0 | 3 | 2 | 1 | 66.7% | 2,317.71 | 597,182.56 | 174,172.30 | 3.35 | 176.57 | 0.79 | 0.79 | 138.60 | 772.57 | 2,934.86 | -1,006.46 | 1 | 1 | 1 | 1 | 1 | 100,000.00 | 102,317.71 | 2.32% | 40.80 | 1 |
| 2026-08 | 11 | 11 | 0 | 9 | 5 | 4 | 55.6% | 9,644.02 | 1,862,881.67 | 532,422.00 | 3.41 | 336.26 | 1.05 | 1.05 | 145.43 | 1,071.56 | 3,175.86 | -1,107.44 | 3 | 2 | 3 | 4 | 2 | 100,000.00 | 109,644.02 | 9.64% | 151.21 | 2 |

Simulated research only. No orders were placed.
