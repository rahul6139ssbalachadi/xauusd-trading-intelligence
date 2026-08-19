-- market_data table (Section 26). SQLite dialect, matches db/trading.db
-- (config/settings.toml paths.db).
--
-- ts_broker_epoch stores the raw integer `time` field exactly as MT5's
-- copy_rates_range/copy_rates_from_pos return it (epoch seconds). This
-- is intentionally NOT converted or relabeled as UTC here.
--
-- OFFSET VERIFIED 2026-08-18 against live XM Global MT5 terminal
-- (GOLD.i#, demo login 345982869): symbol_info_tick().time minus UTC
-- now = +3.00h -> broker runs EEST (UTC+3) in summer, EET (UTC+2) in
-- winter (DST). Therefore these epochs are BROKER-SERVER TIME, not UTC.
-- To get true UTC: subtract 3h in summer / 2h in winter. Label hour-
-- based profiles as broker time and apply the offset where true-UTC
-- alignment matters. Do NOT relabel the stored epoch as UTC.
CREATE TABLE market_data (
    symbol            TEXT    NOT NULL,
    timeframe         TEXT    NOT NULL,
    source            TEXT    NOT NULL,  -- provider name, e.g. "mt5", "dukascopy", "histdata" -- lets bars from different providers coexist for the same symbol/timeframe/timestamp
    ts_broker_epoch   INTEGER NOT NULL,  -- raw MT5 epoch seconds; offset/timezone UNVERIFIED, do not assume UTC
    open              REAL    NOT NULL,
    high              REAL    NOT NULL,
    low               REAL    NOT NULL,
    close             REAL    NOT NULL,
    tick_volume       INTEGER NOT NULL,  -- MT5's tick_volume field, not a renamed "volume"
    spread            INTEGER,           -- MT5's spread field; nullable -- not yet captured by MT5Provider.get_ohlcv(), which only maps time/open/high/low/close/tick_volume

    PRIMARY KEY (symbol, timeframe, source, ts_broker_epoch),

    CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CHECK (high >= low),
    CHECK (high >= open AND high >= close),
    CHECK (low <= open AND low <= close),
    CHECK (tick_volume >= 0)
);
