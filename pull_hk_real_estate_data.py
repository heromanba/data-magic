from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd


PRICE_URL = "https://www.rvd.gov.hk/datagovhk/1.2M.csv"
RENT_URL = "https://www.rvd.gov.hk/datagovhk/1.1M.csv"
OUTPUT_FILE = Path("hong_kong_real_estate_monthly_recent.csv")
RECENT_START_YEAR = 2019

USER_AGENT = "data-magic-hk-real-estate-downloader/1.0"

VALUE_COLUMNS = ["Hong Kong", "Kowloon", "New Territories"]
CLASS_ORDER = ["Class A", "Class B", "Class C", "Class D", "Class E"]


@dataclass(frozen=True)
class SourceSpec:
    market: str
    url: str


def fetch_csv(url: str) -> pd.DataFrame:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        raw = response.read()
    return pd.read_csv(BytesIO(raw), header=1)


def tidy_series(df: pd.DataFrame, market: str) -> pd.DataFrame:
    result = df.copy()
    result["month"] = pd.to_datetime(result["Month"], format="%m-%Y", errors="coerce")

    records = []
    for class_name in CLASS_ORDER:
        for region in VALUE_COLUMNS:
            value_col = f"{class_name} {region}"
            remarks_col = f"{value_col} - Remarks"

            if value_col not in result.columns:
                continue

            subset = result[["month", value_col]].copy()
            subset = subset.rename(columns={value_col: "value"})
            subset["market"] = market
            subset["property_class"] = class_name
            subset["region"] = region

            if remarks_col in result.columns:
                subset["remarks"] = result[remarks_col]
            else:
                subset["remarks"] = pd.NA

            records.append(subset)

    long_df = pd.concat(records, ignore_index=True)
    long_df = long_df.dropna(subset=["month"]).sort_values(["month", "market", "property_class", "region"])
    long_df["year"] = long_df["month"].dt.year
    long_df["month_str"] = long_df["month"].dt.strftime("%Y-%m")
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    return long_df.reset_index(drop=True)


def main() -> None:
    sources: Iterable[SourceSpec] = (
        SourceSpec("price", PRICE_URL),
        SourceSpec("rent", RENT_URL),
    )

    frames = []
    for source in sources:
        print(f"Downloading {source.market} data from {source.url} ...")
        wide_df = fetch_csv(source.url)
        frames.append(tidy_series(wide_df, source.market))

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.loc[combined["year"] >= RECENT_START_YEAR].copy()

    combined = combined[
        ["month", "month_str", "year", "market", "property_class", "region", "value", "remarks"]
    ]

    combined.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(combined):,} rows to {OUTPUT_FILE}")
    print(f"Date range: {combined['month_str'].min()} to {combined['month_str'].max()}")
    print("Markets:", ", ".join(sorted(combined["market"].unique())))
    print("Classes:", ", ".join(sorted(combined["property_class"].unique())))


if __name__ == "__main__":
    main()