"""
Multi-index universe management. Phase 2: instead of one static ticker
list, this maintains a registry of indexes -- major (S&P 500, Nasdaq-100,
Dow 30) and sector (11 GICS sectors, derived from the S&P 500's own sector
classification) -- each with its own dynamically-fetched current
constituent list.

Design choice: sector indexes are NOT a separate data pull. Wikipedia's
S&P 500 constituents table already includes a "GICS Sector" column per
company, so sector universes are just that same pull, grouped by sector.
This keeps sector breadth consistent with the S&P 500 breadth (same
underlying universe, no separate data source to drift out of sync) and
avoids needing a second scrape per sector.

IMPORTANT (survivorship bias): all fetchers here return CURRENT
constituents. Same caveat as phase 1 -- backtesting against years of
history using today's membership excludes stocks removed from the index,
which inflates historical breadth readings. See the module-level note at
the bottom for options if you need point-in-time accuracy.
"""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

logger = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
DOW30_WIKI_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"

# Wikipedia (and many sites) reject requests with the generic User-Agent
# that urllib/pandas sends by default -- pd.read_html(url) has no way to
# set headers, and this returns an HTTP 403 that's easy to mistake for a
# "the page changed" failure. Fetch the HTML ourselves with a normal
# browser-like User-Agent, then hand the text to pd.read_html.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _read_html_tables(url: str) -> list[pd.DataFrame]:
    """pd.read_html with an explicit User-Agent (see module note above),
    a real timeout, and an error message that names the URL and status
    code rather than a bare traceback."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Failed to fetch {url} ({e}). If this is a 403, Wikipedia may be "
            f"rate-limiting the runner's IP -- retry, or check the URL is still valid."
        ) from e
    return pd.read_html(io.StringIO(resp.text))

# GICS sector names as they appear in the S&P 500 Wikipedia table.
GICS_SECTORS = [
    "Information Technology",
    "Health Care",
    "Financials",
    "Consumer Discretionary",
    "Communication Services",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Utilities",
    "Real Estate",
    "Materials",
]


def _clean_ticker(t: str) -> str:
    return t.strip().replace(".", "-")


def fetch_sp500_with_sectors() -> pd.DataFrame:
    """Returns DataFrame [ticker, sector] for the current S&P 500."""
    tables = _read_html_tables(SP500_WIKI_URL)
    df = tables[0]
    if "Symbol" not in df.columns or "GICS Sector" not in df.columns:
        raise RuntimeError(
            "S&P 500 Wikipedia table is missing expected 'Symbol'/'GICS Sector' "
            f"columns (found: {list(df.columns)}) -- page layout may have changed."
        )
    out = pd.DataFrame({
        "ticker": df["Symbol"].map(_clean_ticker),
        "sector": df["GICS Sector"],
    })
    return out.drop_duplicates(subset="ticker").reset_index(drop=True)


def _find_ticker_table(
    tables: list[pd.DataFrame],
    candidates: tuple[str, ...],
    expected_rows: range | None = None,
    context: str = "",
) -> tuple[pd.DataFrame, str]:
    """Search `tables` for one containing a column matching any name in
    `candidates` (case/whitespace-insensitive), optionally constrained to
    a plausible row count. Returns (table, matched_column_name).

    On failure, logs every table's shape and column names at INFO level --
    this is the actual diagnostic you need when Wikipedia renames a column
    (as happened with the Nasdaq-100 page), so the next failure is visible
    directly in the workflow log instead of requiring another round trip.
    """
    normalized_candidates = {c.strip().lower() for c in candidates}
    best_match = None

    for t in tables:
        col_map = {str(c).strip(): str(c).strip() for c in t.columns}
        for col in col_map:
            if col.strip().lower() in normalized_candidates:
                if expected_rows is None or len(t) in expected_rows:
                    return t, col
                # Right column name but implausible row count -- keep as a
                # fallback in case no better match is found.
                if best_match is None:
                    best_match = (t, col)

    if best_match is not None:
        logger.warning("%s: matched column but row count was outside the expected "
                        "range (%d rows) -- using it anyway, verify the result.",
                        context, len(best_match[0]))
        return best_match

    logger.info("%s: no matching table found. Tables on the page:", context)
    for i, t in enumerate(tables):
        logger.info("  table[%d]: %d rows, columns=%s", i, len(t), list(t.columns))
    raise RuntimeError(
        f"Could not locate the {context} constituents table -- page layout has "
        f"likely changed (tried column names: {candidates}). See the INFO log "
        f"lines above this error for every table's actual columns on the page; "
        f"update the `candidates` tuple in src/ingestion/universe.py to match."
    )


def fetch_nasdaq100() -> list[str]:
    """Returns current Nasdaq-100 constituent tickers."""
    tables = _read_html_tables(NASDAQ100_WIKI_URL)
    table, col = _find_ticker_table(
        tables,
        candidates=("Ticker", "Symbol", "Ticker symbol", "Ticker Symbol"),
        expected_rows=range(90, 115),  # ~100 components, some share classes
        context="Nasdaq-100",
    )
    return sorted({_clean_ticker(x) for x in table[col].dropna()})


def fetch_dow30() -> list[str]:
    """Returns current Dow Jones Industrial Average constituent tickers."""
    tables = _read_html_tables(DOW30_WIKI_URL)
    table, col = _find_ticker_table(
        tables,
        candidates=("Symbol", "Ticker", "Ticker symbol", "Ticker Symbol"),
        expected_rows=range(20, 35),  # DJIA has 30 components
        context="Dow 30",
    )
    return sorted({_clean_ticker(x) for x in table[col].dropna()})


def fetch_russell2000() -> list[str]:
    """Not implemented: no reliable free source for the full, current
    ~2000-ticker Russell 2000 constituent list. Wikipedia does not
    maintain a complete member list, and most free APIs cap constituent
    endpoints. Options if you need this:
      - iShares IWM ETF holdings CSV (ishares.com publishes daily holdings
        for its own funds without an API key) -- format changes
        periodically, would need its own parser.
      - A paid data vendor (Polygon.io, Norgate) with an index membership
        endpoint.
    Left unimplemented rather than silently returning a wrong/partial list.
    """
    raise NotImplementedError(
        "Russell 2000 constituent list has no reliable free full source. "
        "See function docstring for options."
    )


# Registry: index_key -> metadata + fetch function. index_key is what's
# stored in breadth_daily.index_key and shown in the dashboard selector.
def _sector_index_key(sector_name: str) -> str:
    return "sector_" + sector_name.lower().replace(" ", "_")


MAJOR_INDEXES = {
    "sp500": {"label": "S&P 500", "type": "major", "fetch": lambda: fetch_sp500_with_sectors()["ticker"].tolist()},
    "nasdaq100": {"label": "Nasdaq-100", "type": "major", "fetch": fetch_nasdaq100},
    "dow30": {"label": "Dow Jones Industrial Average", "type": "major", "fetch": fetch_dow30},
    # "russell2000" intentionally omitted -- see fetch_russell2000 docstring.
}


def build_full_registry() -> dict[str, dict]:
    """Builds the complete index registry, including sector indexes
    derived from the S&P 500 pull. Sector fetchers are closures over a
    single shared S&P 500 pull so we don't re-fetch Wikipedia once per
    sector -- call this once, then use registry[key]['tickers'] rather
    than calling registry[key]['fetch']() repeatedly."""
    sp500 = fetch_sp500_with_sectors()

    registry = {
        "sp500": {"label": "S&P 500", "type": "major", "tickers": sp500["ticker"].tolist()},
        "nasdaq100": {"label": "Nasdaq-100", "type": "major", "tickers": fetch_nasdaq100()},
        "dow30": {"label": "Dow Jones Industrial Average", "type": "major", "tickers": fetch_dow30()},
    }

    for sector in GICS_SECTORS:
        key = _sector_index_key(sector)
        tickers = sp500.loc[sp500["sector"] == sector, "ticker"].tolist()
        if not tickers:
            logger.warning("No S&P 500 tickers found for sector '%s' -- check GICS_SECTORS "
                            "spelling still matches Wikipedia's current column values.", sector)
            continue
        registry[key] = {"label": f"Sector: {sector}", "type": "sector", "tickers": tickers}

    return registry


# NOTE on point-in-time constituents for unbiased backtesting: see phase 1
# note (unchanged) -- free sources give current membership only. Accept
# survivorship bias for prototyping, document it in any backtest report,
# or pay for point-in-time membership (Polygon.io, Norgate, CRSP) before
# trusting a backtest for live decisions.
