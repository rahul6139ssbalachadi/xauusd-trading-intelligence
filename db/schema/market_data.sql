-- market_data table (Section 26). SQLite dialect, matches db/trading.db
-- (config/settings.toml paths.db).
--
-- ts_broker_epoch stores the raw integer `time` field exactly as MT5's
-- copy_rates_range/copy_rates_from_pos return it (epoch seconds). This
-- is intentionally NOT converted or relabeled as UTC here: MT5Provider
-- (market_data/providers/mt5_provider.py) currently treats this epoch
-- as UTC via pd.to_datetime(..., utc=True), but that assumption has
-- NOT been independently verified against a known-UTC reference --
-- MT5/broker servers commonly report time in broker-server time (e.g.
-- EET/EEST, UTC+2/+3), not true UTC. Until that offset is confirmed,
-- the schema stores the untouched broker epoch and nothing else, so no
-- incorrect conversion gets baked into stored data.
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
