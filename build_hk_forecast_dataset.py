from __future__ import annotations

import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd


USER_AGENT = "data-magic-hk-forecast-builder/1.0"
OUTPUT_DIR = Path("data_outputs")
TRANSACTION_OUTPUT = OUTPUT_DIR / "hk_apartment_transactions_enriched.csv"
PANEL_OUTPUT = OUTPUT_DIR / "hk_apartment_monthly_forecast_panel.csv"


@dataclass(frozen=True)
class Source:
    label: str
    url: str


TRANSACTION_SOURCES: tuple[Source, ...] = (
    Source("HOS 2018", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-018"),
    Source("HOS 2019", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-019"),
    Source("HOS 2020", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-020"),
    Source("HOS 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-022"),
    Source("HOS 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-023"),
    Source("HOS 2024", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-024"),
    Source("GSH 2019", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g19"),
    Source("GSH 2020", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g20"),
    Source("GSH 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g22"),
    Source("GSH 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g23"),
    Source("GSH 2024", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-g24"),
    Source("EFAS 2022", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-e22"),
    Source("EFAS 2023", "https://data.housingauthority.gov.hk/psi/rest/export/ssfs-sale-transactions-e23"),
)

COURTS_URL = "https://data.housingauthority.gov.hk/psi/rest/export/hos-courts"
RVD_PRICE_URL = "https://www.rvd.gov.hk/datagovhk/1.2M.csv"
RVD_RENT_URL = "https://www.rvd.gov.hk/datagovhk/1.1M.csv"


def fetch_text(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    payload = fetch_text(url)
    if not payload.strip():
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}


def normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def load_transactions() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for source in TRANSACTION_SOURCES:
        print(f"Downloading {source.label} ...")
        payload = fetch_json(source.url)
        rows = payload.get("data", [])
        if not rows:
            continue

        frame = pd.DataFrame(rows)
        if "court_estate_name" not in frame.columns and "court_name" in frame.columns:
            frame["court_estate_name"] = frame["court_name"]
        if "wing" not in frame.columns:
            frame["wing"] = pd.NA

        frame["source_label"] = source.label
        frame["source_id"] = source.url.rsplit("/", 1)[-1]
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)
    df = df.rename(
        columns={
            "asp_sign_date": "asp_sign_date",
            "court_estate_name": "court_estate_name",
            "block_name": "block_name",
            "floor_num": "floor_num",
            "flat_num": "flat_num",
            "saleable_area": "saleable_area",
            "sale_price": "sale_price",
            "scheme_type": "scheme_type",
            "scheme_name": "scheme_name",
        }
    )

    df["asp_date"] = pd.to_datetime(df["asp_sign_date"], dayfirst=True, errors="coerce")
    df["sale_year"] = df["asp_date"].dt.year
    df["sale_month"] = df["asp_date"].dt.month
    df["sale_month_period"] = df["asp_date"].dt.to_period("M").dt.to_timestamp()
    df["saleable_area"] = pd.to_numeric(df["saleable_area"], errors="coerce")
    df["sale_price"] = pd.to_numeric(df["sale_price"], errors="coerce")
    df["price_per_sq_m"] = df["sale_price"] / df["saleable_area"]
    df["floor_num_num"] = pd.to_numeric(df["floor_num"], errors="coerce")
    df["flat_num_num"] = pd.to_numeric(df["flat_num"], errors="coerce")
    df["join_key"] = df["court_estate_name"].map(normalize_name)

    return df


def load_courts() -> pd.DataFrame:
    payload = fetch_json(COURTS_URL)
    frame = pd.DataFrame(payload.get("data", []))
    frame["join_key"] = frame["estate_name"].map(normalize_name)
    frame["year_of_completion"] = pd.to_numeric(frame["year_of_completion"], errors="coerce")
    frame["map_latitude"] = pd.to_numeric(frame["map_latitude"], errors="coerce")
    frame["map_longitude"] = pd.to_numeric(frame["map_longitude"], errors="coerce")

    frame["no_of_blocks"] = pd.to_numeric(frame["no_of_blocks"], errors="coerce")
    frame["no_of_flats"] = pd.to_numeric(frame["no_of_flats"].astype(str).str.replace(" ", "", regex=False), errors="coerce")
    return frame


def load_rvd_series(url: str, value_column: str) -> pd.DataFrame:
    payload = fetch_text(url)
    wide = pd.read_csv(BytesIO(payload), header=1)
    keep_cols = ["Month", "Class A Hong Kong"]
    available = [c for c in keep_cols if c in wide.columns]
    frame = wide[available].copy()
    frame["sale_month_period"] = pd.to_datetime(frame["Month"], format="%m-%Y", errors="coerce")
    frame[value_column] = pd.to_numeric(frame.get("Class A Hong Kong"), errors="coerce")
    frame = frame[["sale_month_period", value_column]].dropna(subset=["sale_month_period"]).drop_duplicates("sale_month_period")
    return frame


def add_history_features(df: pd.DataFrame) -> pd.DataFrame:
    estate_month = (
        df.groupby(["join_key", "sale_month_period"], as_index=False)
        .agg(
            estate_txn_count=("sale_price", "size"),
            estate_median_price=("sale_price", "median"),
            estate_median_psm=("price_per_sq_m", "median"),
            estate_mean_floor=("floor_num_num", "mean"),
            estate_mean_area=("saleable_area", "mean"),
        )
        .sort_values(["join_key", "sale_month_period"])
    )

    by_estate = estate_month.groupby("join_key")
    estate_month["lag_price_1m"] = by_estate["estate_median_price"].shift(1)
    estate_month["lag_price_3m"] = by_estate["estate_median_price"].shift(3)
    estate_month["lag_price_6m"] = by_estate["estate_median_price"].shift(6)
    estate_month["lag_psm_1m"] = by_estate["estate_median_psm"].shift(1)
    estate_month["lag_psm_3m"] = by_estate["estate_median_psm"].shift(3)
    estate_month["lag_psm_6m"] = by_estate["estate_median_psm"].shift(6)

    estate_month["rolling_price_3m"] = by_estate["estate_median_price"].transform(lambda s: s.shift(1).rolling(3).mean())
    estate_month["rolling_psm_3m"] = by_estate["estate_median_psm"].transform(lambda s: s.shift(1).rolling(3).mean())
    estate_month["rolling_txn_3m"] = by_estate["estate_txn_count"].transform(lambda s: s.shift(1).rolling(3).mean())

    estate_month["future_price_3m"] = by_estate["estate_median_price"].shift(-3)
    estate_month["future_price_6m"] = by_estate["estate_median_price"].shift(-6)
    estate_month["future_price_12m"] = by_estate["estate_median_price"].shift(-12)

    estate_month["future_psm_3m"] = by_estate["estate_median_psm"].shift(-3)
    estate_month["future_psm_6m"] = by_estate["estate_median_psm"].shift(-6)
    estate_month["future_psm_12m"] = by_estate["estate_median_psm"].shift(-12)

    estate_month["future_return_3m"] = estate_month["future_price_3m"] / estate_month["estate_median_price"] - 1
    estate_month["future_return_6m"] = estate_month["future_price_6m"] / estate_month["estate_median_price"] - 1
    estate_month["future_return_12m"] = estate_month["future_price_12m"] / estate_month["estate_median_price"] - 1

    return estate_month


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    tx = load_transactions()
    tx = tx.loc[tx["asp_date"].notna()].copy()

    courts = load_courts()
    merge_cols = [
        "join_key",
        "estate_name",
        "district_name",
        "region_name",
        "year_of_completion",
        "type_of_block",
        "no_of_blocks",
        "no_of_flats",
        "map_latitude",
        "map_longitude",
    ]
    tx = tx.merge(courts[merge_cols], on="join_key", how="left")
    tx = tx.rename(columns={"estate_name": "lookup_estate_name"})
    tx["building_age_at_sale"] = tx["sale_year"] - tx["year_of_completion"]
    tx.loc[tx["building_age_at_sale"] < 0, "building_age_at_sale"] = pd.NA

    price_series = load_rvd_series(RVD_PRICE_URL, "rvd_price_classA_hk")
    rent_series = load_rvd_series(RVD_RENT_URL, "rvd_rent_classA_hk")
    market = price_series.merge(rent_series, on="sale_month_period", how="outer").sort_values("sale_month_period")
    market["market_price_mom"] = market["rvd_price_classA_hk"].pct_change()
    market["market_price_yoy"] = market["rvd_price_classA_hk"].pct_change(12)
    market["market_rent_mom"] = market["rvd_rent_classA_hk"].pct_change()
    market["market_rent_yoy"] = market["rvd_rent_classA_hk"].pct_change(12)

    tx = tx.merge(market, on="sale_month_period", how="left")

    history_panel = add_history_features(tx)

    tx.to_csv(TRANSACTION_OUTPUT, index=False)
    history_panel.to_csv(PANEL_OUTPUT, index=False)

    print(f"Saved enriched transactions: {TRANSACTION_OUTPUT} ({len(tx):,} rows, {tx.shape[1]} cols)")
    print(f"Saved forecast panel: {PANEL_OUTPUT} ({len(history_panel):,} rows, {history_panel.shape[1]} cols)")
    print("Forecast targets available (non-null counts):")
    for col in ["future_price_3m", "future_price_6m", "future_price_12m", "future_return_3m", "future_return_6m", "future_return_12m"]:
        print(f"- {col}: {history_panel[col].notna().sum():,}")


if __name__ == "__main__":
    main()