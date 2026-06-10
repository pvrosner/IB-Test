"""Load Databento OHLCV-1m parquet and build a continuous front-month series."""

import pandas as pd

ET = "America/New_York"


def load_continuous(path: str) -> pd.DataFrame:
    """Return a continuous front-month 1m series indexed by ET timestamp.

    The raw file contains every listed NQ outright and calendar spread.
    For each ET trading date we keep the outright contract with the most
    volume that date (volume-based roll, no back-adjustment -- IB levels
    are intraday so roll gaps between days don't matter).
    """
    df = pd.read_parquet(path, columns=["open", "high", "low", "close", "volume", "symbol"])
    df = df[~df["symbol"].str.contains("-")]
    df.index = df.index.tz_convert(ET)
    df = df.sort_index()

    # trading date: ET date works because the engine only uses 9:30-16:00 ET
    dates = df.index.date
    vol = df.groupby([dates, "symbol"], observed=True)["volume"].sum()
    front = vol.groupby(level=0).idxmax().map(lambda t: t[1])

    keep = df["symbol"].values == front.reindex(dates).values
    out = df[keep].drop(columns="symbol")
    out = out[~out.index.duplicated(keep="first")]
    return out


def rth_days(df: pd.DataFrame):
    """Yield (date, ib_bars, post_bars) for each regular session.

    ib_bars   : 09:30:00-10:29:59 ET (the initial balance hour)
    post_bars : 10:30:00-15:59:59 ET (the rest of the regular session)

    Days with an incomplete IB hour or a short post-IB session
    (holidays, half days) are skipped.
    """
    minutes = df.index.hour * 60 + df.index.minute
    rth = df[(minutes >= 570) & (minutes < 960)]
    for date, day in rth.groupby(rth.index.date):
        m = day.index.hour * 60 + day.index.minute
        ib = day[m < 630]
        post = day[m >= 630]
        if len(ib) >= 55 and len(post) >= 240:
            yield date, ib, post
