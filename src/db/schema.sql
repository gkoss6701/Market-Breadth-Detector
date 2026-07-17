-- Raw daily OHLCV, source of truth for recomputation. Shared across all
-- indexes -- a ticker that belongs to multiple indexes (e.g. AAPL is in
-- sp500, nasdaq100, and sector_information_technology) is only stored once.
CREATE TABLE IF NOT EXISTS prices (
    date DATE NOT NULL,
    ticker TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    PRIMARY KEY (date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

-- Current index membership. Refreshed periodically (constituent lists
-- change infrequently -- weekly refresh is more than enough) rather than
-- on every daily run. index_key examples: 'sp500', 'nasdaq100', 'dow30',
-- 'sector_information_technology', 'sector_energy', etc.
CREATE TABLE IF NOT EXISTS index_constituents (
    index_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_key, ticker)
);

CREATE INDEX IF NOT EXISTS idx_constituents_index_key ON index_constituents(index_key);

-- Human-readable labels + type ('major' | 'sector') per index_key, so the
-- dashboard selector doesn't need to hardcode display names.
CREATE TABLE IF NOT EXISTS index_metadata (
    index_key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    index_type TEXT NOT NULL  -- 'major' | 'sector'
);

-- Daily computed breadth metrics + composite score/regime, now scoped
-- per index via index_key. One row per (index_key, date).
CREATE TABLE IF NOT EXISTS breadth_daily (
    index_key TEXT NOT NULL,
    date DATE NOT NULL,
    pct_above_20ma REAL,
    pct_above_50ma REAL,
    pct_above_200ma REAL,
    ad_line REAL,
    new_highs INTEGER,
    new_lows INTEGER,
    up_down_vol_ratio REAL,
    composite_score REAL,
    regime TEXT,
    bearish_divergence BOOLEAN DEFAULT 0,
    bullish_divergence BOOLEAN DEFAULT 0,
    PRIMARY KEY (index_key, date)
);

CREATE INDEX IF NOT EXISTS idx_breadth_daily_index_key ON breadth_daily(index_key);
CREATE INDEX IF NOT EXISTS idx_breadth_daily_date ON breadth_daily(date);

-- Alert log, so repeated regime flips don't spam duplicate texts and so
-- you have an audit trail of what fired and when. Now index_key-scoped
-- since each index can flip regime independently.
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_key TEXT NOT NULL,
    date DATE NOT NULL,
    alert_type TEXT NOT NULL,   -- 'regime_flip' | 'bearish_divergence' | 'bullish_divergence'
    detail TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
