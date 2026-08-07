from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

import pandas as pd


OUTPUT_FILE = Path("hong_kong_housing_transactions_recent.csv")
USER_AGENT = "data-magic-hk-transaction-downloader/1.0"


@dataclass(frozen=True)
class TransactionSource:
    label: str
    url: str


TRANSACTION_SOURCES: tuple[TransactionSource, ...] = (
    TransactionSource("HOS 2018", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-018"),
    TransactionSource("HOS 2019", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-019"),
    TransactionSource("HOS 2020", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-020"),
    TransactionSource("HOS 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-022"),
    TransactionSource("HOS 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-023"),
    TransactionSource("HOS 2024", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-024"),
    TransactionSource("GSH 2018", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g18"),
    TransactionSource("GSH 2019", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g19"),
    TransactionSource("GSH 2020", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g20"),
    TransactionSource("GSH 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g22"),
    TransactionSource("GSH 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g23"),
    TransactionSource("GSH 2024", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g24"),
    TransactionSource("EFAS 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-e22"),
    TransactionSource("EFAS 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-e23"),
)

COURTS_URL = "https://data.housingauthority.gov.hk/psi/rest/export/hos-courts"


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=90) as response:
        payload = response.read()
    if not payload.strip():
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}


def normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def load_transactions(source: TransactionSource) -> pd.DataFrame:
    payload = fetch_json(source.url)
    records = payload.get("data", [])
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame

    frame = frame.rename(
        columns={
            "scheme_type": "scheme_type",
            "scheme_name": "scheme_name",
            "asp_sign_date": "asp_sign_date",
            "court_estate_name": "court_estate_name",
            "block_name": "block_name",
            "wing": "wing",
            "floor_num": "floor_num",
            "flat_num": "flat_num",
            "saleable_area": "saleable_area",
            "sale_price": "sale_price",
        }
    )
    frame["source_label"] = source.label
    frame["source_id"] = source.url.rsplit("/", 1)[-1]
    frame["asp_date"] = pd.to_datetime(frame["asp_sign_date"], dayfirst=True, errors="coerce")
    frame["sale_year"] = frame["asp_date"].dt.year
    frame["sale_month"] = frame["asp_date"].dt.month
    frame["saleable_area"] = pd.to_numeric(frame["saleable_area"], errors="coerce")
    frame["sale_price"] = pd.to_numeric(frame["sale_price"], errors="coerce")
    frame["price_per_sq_m"] = frame["sale_price"] / frame["saleable_area"]
    frame["floor_num_num"] = pd.to_numeric(frame["floor_num"], errors="coerce")
    frame["flat_num_num"] = pd.to_numeric(frame["flat_num"], errors="coerce")
    return frame


def load_courts() -> pd.DataFrame:
    payload = fetch_json(COURTS_URL)
    frame = pd.DataFrame(payload.get("data", []))
    if frame.empty:
        return frame

    frame["join_key"] = frame["estate_name"].map(normalize_name)
    frame["year_of_completion_num"] = pd.to_numeric(frame["year_of_completion"], errors="coerce")
    frame["no_of_blocks_num"] = pd.to_numeric(frame["no_of_blocks"], errors="coerce")
    frame["no_of_flats_num"] = pd.to_numeric(frame["no_of_flats"].astype(str).str.replace(" ", "", regex=False), errors="coerce")
    frame["saleable_area_low"] = pd.to_numeric(frame["saleable_area_of_flats"].astype(str).str.extract(r"^(\d+(?:\.\d+)?)")[0], errors="coerce")
    frame["saleable_area_high"] = pd.to_numeric(frame["saleable_area_of_flats"].astype(str).str.extract(r"-\s*(\d+(?:\.\d+)?)")[0], errors="coerce")
    frame["initial_sale_price_low"] = pd.to_numeric(frame["initial_sale_price"].astype(str).str.replace(",", "", regex=False).str.extract(r"^(\d+(?:\.\d+)?)")[0], errors="coerce")
    frame["initial_sale_price_high"] = pd.to_numeric(frame["initial_sale_price"].astype(str).str.replace(",", "", regex=False).str.extract(r"-\s*(\d+(?:\.\d+)?)")[0], errors="coerce")
    return frame


def build_dataset() -> pd.DataFrame:
    frames = []
    for source in TRANSACTION_SOURCES:
        print(f"Downloading {source.label} ...")
        frame = load_transactions(source)
        if not frame.empty:
            frames.append(frame)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.loc[combined["sale_year"] >= 2018].copy()

    courts = load_courts()
    courts = courts[
        [
            "join_key",
            "estate_name",
            "district_name",
            "region_name",
            "year_of_completion_num",
            "type_of_block",
            "no_of_blocks_num",
            "no_of_flats_num",
            "saleable_area_low",
            "saleable_area_high",
            "initial_sale_price_low",
            "initial_sale_price_high",
        ]
    ].rename(
        columns={
            "estate_name": "lookup_estate_name",
            "district_name": "district_name",
            "region_name": "region_name",
            "year_of_completion_num": "year_of_completion",
            "type_of_block": "type_of_block",
            "no_of_blocks_num": "no_of_blocks",
            "no_of_flats_num": "no_of_flats",
            "saleable_area_low": "court_saleable_area_low",
            "saleable_area_high": "court_saleable_area_high",
            "initial_sale_price_low": "court_initial_sale_price_low",
            "initial_sale_price_high": "court_initial_sale_price_high",
        }
    )

    combined["join_key"] = combined["court_estate_name"].map(normalize_name)
    combined = combined.merge(courts, on="join_key", how="left")

    combined["building_age_at_sale"] = combined["sale_year"] - combined["year_of_completion"]
    combined.loc[combined["building_age_at_sale"] < 0, "building_age_at_sale"] = pd.NA

    combined = combined[
        [
            "source_label",
            "scheme_type",
            "scheme_name",
            "asp_date",
            "sale_year",
            "sale_month",
            "court_estate_name",
            "lookup_estate_name",
            "district_name",
            "region_name",
            "year_of_completion",
            "building_age_at_sale",
            "type_of_block",
            "no_of_blocks",
            "no_of_flats",
            "block_name",
            "wing",
            "floor_num",
            "floor_num_num",
            "flat_num",
            "flat_num_num",
            "saleable_area",
            "sale_price",
            "price_per_sq_m",
            "court_saleable_area_low",
            "court_saleable_area_high",
            "court_initial_sale_price_low",
            "court_initial_sale_price_high",
        ]
    ]

    return combined.sort_values(["asp_date", "court_estate_name", "block_name", "flat_num"]).reset_index(drop=True)


def main() -> None:
    dataset = build_dataset()
    dataset.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(dataset):,} rows to {OUTPUT_FILE}")
    print(f"Date range: {dataset['asp_date'].min().date()} to {dataset['asp_date'].max().date()}")
    print("Scheme types:", ", ".join(sorted(dataset["scheme_type"].dropna().unique())))
    print("Districts:", ", ".join(sorted(dataset["district_name"].dropna().unique())[:10]))


if __name__ == "__main__":
    main()