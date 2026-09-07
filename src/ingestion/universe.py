"""
Multi-index universe management. Phase 2: instead of one static ticker
list, this maintains a registry of indexes -- major (S&P 500, Nasdaq-100,
Dow 30, S&P 400 Mid Cap, S&P 600 Small Cap, Russell 1000/2000/3000) and
sector (11 GICS sectors, derived from the S&P 500's own sector
classification) -- each with its own dynamically-fetched current
constituent list. S&P 500 and Dow 30 are sourced from Wikipedia;
Nasdaq-100/S&P 400/S&P 600 from SlickCharts (Wikipedia either dropped or
never had a per-company table for these -- see NASDAQ100_SOURCE_URL
below); the three Russell indexes from ChartMill.com's JSON REST API (see
CHARTMILL_BASE_URL below) -- neither Wikipedia nor SlickCharts publishes
a full Russell constituent list.

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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# As of Sept 2026, en.wikipedia.org/wiki/Dow_Jones_Industrial_Average no
# longer carries the component table itself -- it only has historical
# annual-return data. The actual constituent list (Company/Exchange/Symbol/
# Sector/Date added) lives on this separate article instead. Verified
# directly against the live page before changing this constant -- if it
# breaks again, check whether Wikipedia has moved/renamed this article
# rather than assuming the old URL is still right.
DOW30_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_Dow_Jones_Industrial_Average_companies"

# As of Sept 2026, en.wikipedia.org/wiki/Nasdaq-100 no longer has ANY table
# listing individual component tickers (checked directly: it only has
# all-time-highs/annual-returns/milestone tables and sector-weight
# percentages -- no per-company roster at all, and no "List of Nasdaq-100
# companies" companion article exists either, unlike DJIA above). Same
# situation applies to the three Russell indexes below: no reliable free
# Wikipedia source. SlickCharts publishes a plain server-rendered HTML
# table of current Nasdaq-100 members with weights, refreshed regularly,
# and needs no API key -- used here as the replacement source. It's a
# third-party site rather than Wikipedia, so treat its layout as similarly
# liable to drift; the same table/column-detection guard below is what
# catches that if it happens.
NASDAQ100_SOURCE_URL = "https://www.slickcharts.com/nasdaq100"

# S&P 400 (Mid Cap) and S&P 600 (Small Cap) -- added to complement the
# existing S&P 500 / Nasdaq-100 / Dow 30 majors with dedicated mid- and
# small-cap breadth universes. Same vendor and table shape as
# NASDAQ100_SOURCE_URL (a "Symbol" column, clean common-stock-only
# membership -- no Wikipedia equivalent exists for either of these, so
# SlickCharts is the primary source here, not a fallback).
SP400_SOURCE_URL = "https://www.slickcharts.com/sp400"
SP600_SOURCE_URL = "https://www.slickcharts.com/sp600"

# Russell 1000 / 2000 / 3000 -- neither Wikipedia nor SlickCharts publishes
# a full Russell constituent list (checked both directly). ChartMill.com
# does, but its page is a JS-rendered SPA -- a plain requests.get() on the
# page URL itself returns an empty shell, so this doesn't use the
# pd.read_html() approach the rest of this file uses. Instead it hits the
# plain JSON REST API the page's own JavaScript calls to populate the
# table (found via the browser's network log): a slug -> index-id lookup,
# then a paginated members list. Confirmed working from a live browser
# session including with credentials omitted (no login/cookie needed), and
# it's a clean typed JSON API rather than scraped HTML -- no column-name
# guessing needed. What's NOT confirmed: whether ChartMill's Cloudflare
# protection (visible in its network requests) permits unattended requests
# from a plain script with no browser fingerprint, the way it runs on the
# NAS. If it starts blocking, this raises loudly (see
# _fetch_chartmill_index_tickers) rather than returning a wrong/partial
# list -- check the exception and, if needed, fall back to a manual pull
# (open the ChartMill page in a real browser, as was done to build this).
CHARTMILL_BASE_URL = "https://www.chartmill.com/chartmill-rest"
CHARTMILL_REGION_ID = 1  # USA

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


def _clean_ticker(t) -> str | None:
    """Normalize a raw table cell into a ticker string. Returns None for
    anything that isn't real ticker text (NaN/float cells from malformed
    or footnote table rows, empty strings) so callers can filter it out
    instead of crashing -- .map()/.dropna() alone don't guarantee every
    cell is a clean string; Wikipedia tables occasionally have a stray
    blank or footnote row that survives to this point as NaN (a float,
    not a string), which is what caused a bare AttributeError here
    before this guard existed.
    """
    if not isinstance(t, str):
        return None
    t = t.strip()
    if not t:
        return None
    return t.replace(".", "-")


def fetch_sp500_with_sectors() -> pd.DataFrame:
    """Returns DataFrame [ticker, sector] for the current S&P 500."""
    tables = _read_html_tables(SP500_WIKI_URL)
    df = tables[0]
    if "Symbol" not in df.columns or "GICS Sector" not in df.columns:
        raise RuntimeError(
            "S&P 500 Wikipedia table is missing expected 'Symbol'/'GICS Sector' "
            f"columns (found: {list(df.columns)}) -- page layout may have changed."
        )
    df = df.dropna(subset=["Symbol"])
    out = pd.DataFrame({
        "ticker": df["Symbol"].map(_clean_ticker),
        "sector": df["GICS Sector"],
    })
    out = out.dropna(subset=["ticker"])
    return out.drop_duplicates(subset="ticker").reset_index(drop=True)


# Require at least one letter, not just any 1-6 char alnum token: a plain
# `[A-Z0-9]{1,6}` also matches pure-digit strings like "2025", so a stray
# table with a 4-digit "Year" column (e.g. an annual-returns table, which
# is now the only kind of table left on the old DJIA/Nasdaq-100 URLs above)
# could score a near-100% "ticker-like" ratio and get picked over the real
# constituents table by mistake. Tightened defensively while fixing the
# Dow 30 / Nasdaq-100 source URLs above -- content-based detection is only
# a safety net if it actually filters out non-ticker content.
_TICKER_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,2})?$")


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


def _fetch_slickcharts_index(url: str, expected_rows: range, context: str) -> list[str]:
    """Shared fetch path for every SlickCharts-sourced index (Nasdaq-100,
    S&P 400, S&P 600 as of this writing) -- same table shape (a "Symbol"
    column) and the same table/column-detection guard as the
    Wikipedia-sourced fetchers, just pointed at a different site."""
    tables = _read_html_tables(url)
    table, col = _find_ticker_table(
        tables,
        candidates=("Ticker", "Symbol", "Ticker symbol", "Ticker Symbol"),
        expected_rows=expected_rows,
        context=context,
    )
    cleaned = {_clean_ticker(x) for x in table[col].dropna()}
    return sorted(t for t in cleaned if t)


def fetch_nasdaq100() -> list[str]:
    """Returns current Nasdaq-100 constituent tickers.

    Sourced from SlickCharts (see NASDAQ100_SOURCE_URL docstring above),
    not Wikipedia -- Wikipedia's Nasdaq-100 page no longer has a per-company
    table at all as of Sept 2026.
    """
    return _fetch_slickcharts_index(
        NASDAQ100_SOURCE_URL,
        expected_rows=range(95, 115),  # ~100 components, some share classes
        context="Nasdaq-100",
    )


def fetch_sp400() -> list[str]:
    """Returns current S&P 400 (Mid Cap) constituent tickers."""
    return _fetch_slickcharts_index(
        SP400_SOURCE_URL,
        expected_rows=range(390, 410),  # 400 components, some slack for pending changes
        context="S&P 400 (Mid Cap)",
    )


def fetch_sp600() -> list[str]:
    """Returns current S&P 600 (Small Cap) constituent tickers."""
    return _fetch_slickcharts_index(
        SP600_SOURCE_URL,
        expected_rows=range(590, 615),  # 600 components, some slack for pending changes
        context="S&P 600 (Small Cap)",
    )


def fetch_dow30() -> list[str]:
    """Returns current Dow Jones Industrial Average constituent tickers."""
    tables = _read_html_tables(DOW30_WIKI_URL)
    table, col = _find_ticker_table(
        tables,
        candidates=("Symbol", "Ticker", "Ticker symbol", "Ticker Symbol"),
        expected_rows=range(20, 35),  # DJIA has 30 components
        context="Dow 30",
    )
    cleaned = {_clean_ticker(x) for x in table[col].dropna()}
    return sorted(t for t in cleaned if t)


def _fetch_chartmill_index_tickers(slug: str, expected_rows: range, context: str) -> list[str]:
    """Shared fetch path for every ChartMill-sourced index (Russell
    1000/2000/3000 as of this writing). Two calls: resolve the index's
    numeric id from its URL slug, then page through that index's member
    list (600 rows/page as observed; paginated regardless, so this doesn't
    hardcode a page size). Raises RuntimeError/ValueError rather than
    returning a wrong/partial list if ChartMill's API shape changes or
    blocks the request -- see CHARTMILL_BASE_URL docstring above for the
    known caveat (Cloudflare behavior for unattended requests is
    unconfirmed)."""
    try:
        stats_resp = requests.get(
            f"{CHARTMILL_BASE_URL}/security/stats/index-by-slug/{slug}",
            params={"regionId": CHARTMILL_REGION_ID},
            headers=_HEADERS,
            timeout=20,
        )
        stats_resp.raise_for_status()
        index_id = stats_resp.json()["item"]["id"]
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Could not resolve ChartMill index id for {context} (slug '{slug}'): {exc}. "
            f"ChartMill's site/API may have changed, or the request was blocked -- "
            f"see CHARTMILL_BASE_URL docstring in this file."
        ) from exc

    tickers: list[str] = []
    page = 0
    while True:
        resp = requests.get(
            f"{CHARTMILL_BASE_URL}/security/members/index/{index_id}",
            params={"page": page, "regionId": CHARTMILL_REGION_ID},
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        tickers.extend(item["ticker"] for item in data["content"])
        if data.get("last", True):
            break
        page += 1
        if page > 20:  # ~12,000 rows -- safety valve against an infinite loop if "last" is ever wrong
            raise RuntimeError(f"{context}: ChartMill pagination didn't terminate after {page} pages -- aborting.")

    cleaned = sorted({_clean_ticker(t) for t in tickers if _clean_ticker(t)})
    if len(cleaned) not in expected_rows:
        raise ValueError(
            f"{context}: got {len(cleaned)} tickers from ChartMill, expected "
            f"{expected_rows.start}-{expected_rows.stop - 1}. Source data may have "
            f"changed shape -- verify before trusting this list."
        )
    return cleaned


def fetch_russell1000() -> list[str]:
    """Returns current Russell 1000 constituent tickers (large-cap)."""
    return _fetch_chartmill_index_tickers(
        "russell-1000",
        expected_rows=range(950, 1050),  # ~1000 components; ChartMill counts share classes separately (~1013 seen)
        context="Russell 1000",
    )


def fetch_russell2000() -> list[str]:
    """Returns current Russell 2000 constituent tickers (small-cap)."""
    return _fetch_chartmill_index_tickers(
        "russell-2000",
        expected_rows=range(1850, 2000),  # ~2000 components (~1948 seen)
        context="Russell 2000",
    )


def fetch_russell3000() -> list[str]:
    """Returns current Russell 3000 constituent tickers (Russell 1000 +
    Russell 2000, no overlap -- confirmed against live ChartMill data)."""
    return _fetch_chartmill_index_tickers(
        "russell-3000",
        expected_rows=range(2800, 3000),  # ~3000 components (~2961 seen)
        context="Russell 3000",
    )


# Registry: index_key -> metadata + fetch function. index_key is what's
# stored in breadth_daily.index_key and shown in the dashboard selector.
def _sector_index_key(sector_name: str) -> str:
    return "sector_" + sector_name.lower().replace(" ", "_")


# ---------------------------------------------------------------------
# On-disk constituent cache / fallback ("base" lists)
# ---------------------------------------------------------------------
# constituents/<name>.csv holds the last successfully-fetched ticker list
# for each of the eight major-index sources (sp500 also carries a sector
# column -- sector indexes are derived from that same pull, not a
# separate fetch, so caching sp500 covers them too). These files are
# git-tracked and shipped with the repo as a starting "base" so a fresh
# deployment isn't starting from an empty registry, and double as the
# fallback build_full_registry() uses when a live pull fails: a failed
# pull is logged and this falls back to the cache file UNCHANGED -- it's
# only overwritten after a fetch actually succeeds, so one broken source
# (a site layout change, a network blip, a block) never destroys the last
# known-good list, and doesn't stop the other sources from updating
# normally in the same run (contrast with the old behavior, where any
# single fetcher raising aborted build_full_registry() entirely and
# nothing got refreshed that run, even sources that would have
# succeeded).
#
# The three Russell fetchers (ChartMill) are wrapped the same way as the
# other five -- constituents/russell{1000,2000,3000}.csv were generated
# 2026-09-07 the same way as the rest (see constituents/README.md), so
# there's now a base to fall back to. One live-source caveat specific to
# ChartMill: its Cloudflare protection may behave differently for an
# unattended NAS-scheduled request than it did for the interactive
# browser session these were pulled through -- if that turns out to
# block scripted pulls entirely, every Russell refresh will just keep
# falling back to this cache (logged as a warning each time) rather than
# breaking the run, which is exactly the scenario this mechanism exists
# for.
CONSTITUENTS_CACHE_DIR = Path("constituents")


def _cache_path(name: str) -> Path:
    return CONSTITUENTS_CACHE_DIR / f"{name}.csv"


def _read_constituents_cache(name: str) -> pd.DataFrame | None:
    """Returns the cached DataFrame for `name` (columns: ticker[, sector]),
    or None if no usable cache file exists (first run before any
    successful pull, or a corrupt/malformed file)."""
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        logger.warning("Cache file %s exists but couldn't be read as CSV -- treating as unusable.", path)
        return None
    if "ticker" not in df.columns or df["ticker"].dropna().empty:
        logger.warning("Cache file %s has no usable 'ticker' column -- treating as unusable.", path)
        return None
    return df


def _write_constituents_cache(name: str, df: pd.DataFrame) -> None:
    """Overwrites constituents/<name>.csv with a fresh successful pull.
    Writes to a temp file first and renames over the original (atomic on
    POSIX) so a crash mid-write can't leave a truncated cache file behind
    -- the whole point of this cache is to be trustworthy as a fallback."""
    CONSTITUENTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(name)
    tmp_path = path.with_suffix(".csv.tmp")
    df.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _cache_age_str(name: str) -> str:
    try:
        mtime = datetime.fromtimestamp(_cache_path(name).stat().st_mtime, tz=timezone.utc)
        return mtime.strftime("%Y-%m-%d %H:%M UTC")
    except FileNotFoundError:
        return "unknown"


def _fetch_major_index_with_fallback(name: str, fetch_fn, context: str) -> list[str]:
    """Wraps a plain ticker-list fetcher (fetch_nasdaq100/fetch_dow30/
    fetch_sp400/fetch_sp600) with the constituents/ cache -- see the
    CONSTITUENTS_CACHE_DIR comment above for the fallback contract.
    Raises only if the live pull failed AND there's no cache to fall back
    to (matches the pre-cache behavior for that case)."""
    try:
        tickers = fetch_fn()
    except Exception as exc:
        cached = _read_constituents_cache(name)
        if cached is None:
            raise RuntimeError(
                f"{context}: live pull failed ({exc}) and no cached base list exists at "
                f"{_cache_path(name)} -- nothing to fall back to."
            ) from exc
        logger.warning(
            "%s: live pull failed (%s) -- falling back to the cached constituent list at "
            "%s (%d tickers, last refreshed %s). That file was NOT modified by this "
            "failed attempt.",
            context, exc, _cache_path(name), len(cached), _cache_age_str(name),
        )
        return sorted(cached["ticker"].dropna().tolist())
    _write_constituents_cache(name, pd.DataFrame({"ticker": tickers}))
    return tickers


def _fetch_sp500_with_fallback() -> pd.DataFrame:
    """Same fallback contract as _fetch_major_index_with_fallback, for
    fetch_sp500_with_sectors specifically -- it returns ticker+sector, not
    a plain list, since sector indexes are derived from this one pull."""
    try:
        df = fetch_sp500_with_sectors()
    except Exception as exc:
        cached = _read_constituents_cache("sp500")
        if cached is None:
            raise RuntimeError(
                f"S&P 500: live pull failed ({exc}) and no cached base list exists at "
                f"{_cache_path('sp500')} -- nothing to fall back to."
            ) from exc
        if "sector" not in cached.columns:
            raise RuntimeError(
                f"S&P 500: live pull failed ({exc}) and the cached fallback at "
                f"{_cache_path('sp500')} has no 'sector' column, so sector indexes can't "
                f"be derived from it -- not falling back silently to a partial result."
            ) from exc
        logger.warning(
            "S&P 500: live pull failed (%s) -- falling back to the cached constituent "
            "list at %s (%d tickers, last refreshed %s). That file was NOT modified by "
            "this failed attempt; sector indexes for this run are derived from this "
            "cached list too.",
            exc, _cache_path("sp500"), len(cached), _cache_age_str("sp500"),
        )
        return cached
    _write_constituents_cache("sp500", df)
    return df


MAJOR_INDEXES = {
    "sp500": {"label": "S&P 500", "type": "major", "fetch": lambda: fetch_sp500_with_sectors()["ticker"].tolist()},
    "nasdaq100": {"label": "Nasdaq-100", "type": "major", "fetch": fetch_nasdaq100},
    "dow30": {"label": "Dow Jones Industrial Average", "type": "major", "fetch": fetch_dow30},
    "sp400": {"label": "S&P 400 (Mid Cap)", "type": "major", "fetch": fetch_sp400},
    "sp600": {"label": "S&P 600 (Small Cap)", "type": "major", "fetch": fetch_sp600},
    "russell1000": {"label": "Russell 1000", "type": "major", "fetch": fetch_russell1000},
    "russell2000": {"label": "Russell 2000", "type": "major", "fetch": fetch_russell2000},
    "russell3000": {"label": "Russell 3000", "type": "major", "fetch": fetch_russell3000},
}


def build_full_registry() -> dict[str, dict]:
    """Builds the complete index registry, including sector indexes
    derived from the S&P 500 pull. Sector fetchers are closures over a
    single shared S&P 500 pull so we don't re-fetch Wikipedia once per
    sector -- call this once, then use registry[key]['tickers'] rather
    than calling registry[key]['fetch']() repeatedly.

    All eight major sources (sp500 + the four SlickCharts fetchers + the
    three ChartMill/Russell fetchers) go through the constituents/
    cache/fallback wrappers -- a failed live pull for any one of them
    falls back to that source's last known-good cache file and logs a
    warning, rather than raising and aborting the whole registry build
    (which would previously leave every OTHER source's fresh pull
    discarded too, even the ones that succeeded). See the
    CONSTITUENTS_CACHE_DIR comment above."""
    sp500 = _fetch_sp500_with_fallback()

    registry = {
        "sp500": {"label": "S&P 500", "type": "major", "tickers": sp500["ticker"].tolist()},
        "nasdaq100": {"label": "Nasdaq-100", "type": "major",
                      "tickers": _fetch_major_index_with_fallback("nasdaq100", fetch_nasdaq100, "Nasdaq-100")},
        "dow30": {"label": "Dow Jones Industrial Average", "type": "major",
                  "tickers": _fetch_major_index_with_fallback("dow30", fetch_dow30, "Dow 30")},
        "sp400": {"label": "S&P 400 (Mid Cap)", "type": "major",
                  "tickers": _fetch_major_index_with_fallback("sp400", fetch_sp400, "S&P 400 (Mid Cap)")},
        "sp600": {"label": "S&P 600 (Small Cap)", "type": "major",
                  "tickers": _fetch_major_index_with_fallback("sp600", fetch_sp600, "S&P 600 (Small Cap)")},
        "russell1000": {"label": "Russell 1000", "type": "major",
                         "tickers": _fetch_major_index_with_fallback("russell1000", fetch_russell1000, "Russell 1000")},
        "russell2000": {"label": "Russell 2000", "type": "major",
                         "tickers": _fetch_major_index_with_fallback("russell2000", fetch_russell2000, "Russell 2000")},
        "russell3000": {"label": "Russell 3000", "type": "major",
                         "tickers": _fetch_major_index_with_fallback("russell3000", fetch_russell3000, "Russell 3000")},
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
