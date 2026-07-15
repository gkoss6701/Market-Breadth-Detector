"""
Universe management: which tickers count toward breadth calculations.

IMPORTANT (survivorship bias): for backtesting, using TODAY's constituent
list applied to historical data silently excludes stocks that were removed
from the index (usually because they were struggling). This inflates how
healthy breadth looked historically. `get_current_universe()` is fine for
live/forward use. For backtests, prefer a point-in-time snapshot if you can
source one (see NOTE at bottom of file) -- otherwise, document the bias
rather than ignore it.
"""
from __future__ import annotations

import pandas as pd

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def get_current_universe(source: str = "wikipedia") -> list[str]:
    """Return the current list of S&P 500 tickers.

    Free/decent accuracy source. Good enough for live/forward-looking use;
    see module docstring for backtesting caveats.
    """
    if source != "wikipedia":
        raise ValueError(f"Unsupported source: {source}")

    tables = pd.read_html(SP500_WIKI_URL)
    df = tables[0]
    tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
    return sorted(set(tickers))


def load_universe_from_csv(path: str) -> list[str]:
    """Load a static, hand-maintained universe file (data/universe.csv,
    one ticker per line). Recommended for reproducible backtests --
    freeze the list you tested against rather than re-pulling live."""
    with open(path) as f:
        return sorted({line.strip().upper() for line in f if line.strip()})


# NOTE on point-in-time constituents for unbiased backtesting:
# Free sources for historical S&P 500 membership are limited. Options,
# roughly in order of effort/cost:
#   1. Accept survivorship bias, document it in backtest reports.
#   2. Use a community-maintained historical membership CSV (search
#      "S&P 500 historical constituents csv" -- quality varies, verify).
#   3. Pay for point-in-time index membership (Polygon.io, Norgate Data,
#      CRSP) once you're validating a strategy you intend to trade live.
# For the yfinance prototyping phase, option 1 is acceptable -- just say
# so explicitly in any backtest output.
