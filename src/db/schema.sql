-- Raw daily OHLCV, source of truth for recomputation.
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

-- Daily computed breadth metrics + composite score/regime.
CREATE TABLE IF NOT EXISTS breadth_daily (
    date DATE PRIMARY KEY,
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
    bullish_divergence BOOLEAN DEFAULT 0
);

-- Alert log, so repeated regime flips don't spam duplicate texts and so
-- you have an audit trail of what fired and when.
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    alert_type TEXT NOT NULL,   -- 'regime_flip' | 'bearish_divergence' | 'bullish_divergence'
    detail TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
