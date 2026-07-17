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
import re

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


_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,2})?$")


def _flatten_columns(t: pd.DataFrame) -> pd.DataFrame:
    """Wikipedia tables with multi-row headers (e.g. a table with grouped
    'Added'/'Removed' sub-columns) produce a MultiIndex, so a column that
    displays as "Ticker" can internally be a tuple like ('Added', 'Ticker').
    str(the_tuple) won't equal "Ticker", which silently breaks exact-name
    matching -- this is what actually broke the Nasdaq-100 fetch (that
    page has a constituents table AND a separate historical changes table,
    the latter with grouped Added/Removed columns). Flatten defensively so
    every downstream check works against a clean single-level string
    regardless of which shape pandas produced.
    """
    if isinstance(t.columns, pd.MultiIndex):
        t = t.copy()
        t.columns = [
            " ".join(str(p) for p in col if p and "Unnamed" not in str(p)).strip()
            for col in t.columns
        ]
    return t


def _ticker_like_ratio(series: pd.Series) -> float:
    """Fraction of non-null values in `series` that look like a stock
    ticker (1-6 uppercase letters, optional .X/-X share-class suffix)."""
    vals = series.dropna().astype(str).str.strip()
    if len(vals) == 0:
        return 0.0
    matches = vals.map(lambda v: bool(_TICKER_RE.match(v)))
    return matches.mean()


def _find_ticker_table(
    tables: list[pd.DataFrame],
    candidates: tuple[str, ...],
    expected_rows: range | None = None,
    context: str = "",
    min_ticker_ratio: float = 0.7,
) -> tuple[pd.DataFrame, str]:
    """Find the constituents table and its ticker column using TWO
    independent signals, since either alone is fragile on Wikipedia:
      1. Column name matches one of `candidates` (case-insensitive).
      2. The column's actual cell values look like tickers (regex ratio),
         which survives multi-level/tuple column headers that break (1).
    A table is only accepted if its row count is in `expected_rows`
    (when given) AND at least one column clears `min_ticker_ratio` on the
    content check -- name matching alone is not trusted, since Wikipedia
    pages often have more than one table with a plausibly-named column
    (e.g. a historical "Added/Removed" changes table alongside the actual
    constituents table).

    On failure, logs every table's shape/columns AND each column's
    ticker-content ratio at INFO level -- the actual diagnostic needed
    when a page layout changes again.
    """
    normalized_candidates = {c.strip().lower() for c in candidates}
    flattened = [_flatten_columns(t) for t in tables]

    def _best_column(t: pd.DataFrame) -> tuple[str, float] | None:
        best = None
        for col in t.columns:
            ratio = _ticker_like_ratio(t[col])
            name_hint = str(col).strip().lower() in normalized_candidates
            # Require real content signal; name match alone just breaks ties.
            if ratio >= min_ticker_ratio and (best is None or ratio > best[1] or
                                               (ratio == best[1] and name_hint)):
                best = (col, ratio)
        return best

    row_count_matches = []
    for t in flattened:
        if expected_rows is not None and len(t) not in expected_rows:
            continue
        found = _best_column(t)
        if found:
            row_count_matches.append((t, found[0], found[1]))

    if row_count_matches:
        # Prefer the highest ticker-content ratio among tables with a
        # plausible row count.
        row_count_matches.sort(key=lambda x: x[2], reverse=True)
        table, col, ratio = row_count_matches[0]
        logger.info("%s: matched table (%d rows) on column '%s' (%.0f%% ticker-like)",
                    context, len(table), col, ratio * 100)
        return table, col

    # Fall back to content match regardless of row count, so a slightly
    # off `expected_rows` guess doesn't hard-fail the whole run.
    any_matches = [(t, *found) for t in flattened if (found := _best_column(t))]
    if any_matches:
        any_matches.sort(key=lambda x: x[2], reverse=True)
        table, col, ratio = any_matches[0]
        logger.warning("%s: no table matched the expected row count -- using best "
                        "content match anyway (%d rows, column '%s', %.0f%% ticker-like). "
                        "Verify the result.", context, len(table), col, ratio * 100)
        return table, col

    logger.info("%s: no matching table found. Tables on the page:", context)
    for i, t in enumerate(flattened):
        logger.info("  table[%d]: %d rows, columns=%s", i, len(t), list(t.columns))
        for col in t.columns:
            ratio = _ticker_like_ratio(t[col])
            if ratio > 0:
                logger.info("    column '%s': %.0f%% ticker-like", col, ratio * 100)
    raise RuntimeError(
        f"Could not locate the {context} constituents table -- page layout has "
        f"likely changed (tried column names: {candidates}, content-based ticker "
        f"detection also found nothing above {min_ticker_ratio:.0%}). See the INFO "
        f"log lines above this error for every table's columns and ticker-content "
        f"ratios; update src/ingestion/universe.py accordingly."
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
