from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


URL = "https://www.spacious.hk/en/hong-kong/transactions"
OUTPUT_DIR = Path("data_outputs")
OUTPUT_FILE = OUTPUT_DIR / "spacious_transactions_sampled_history.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.spacious.hk/",
}

XHR_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": HEADERS["Accept-Language"],
    "Referer": URL,
    "X-Requested-With": "XMLHttpRequest",
}

ROW_CLASS = "page-city-level-real-estate-transaction-landing-page-desktop-ver--transaction-history-table__table-row"
ROW_CLASS_RE = re.escape(ROW_CLASS)

ROW_PATTERN = re.compile(
    rf'<div class="{ROW_CLASS_RE}"\s+([^>]*)>(.*?)</div>\s*(?=<div class="{ROW_CLASS_RE}"|$)',
    re.IGNORECASE | re.DOTALL,
)

ATTR_PATTERN = re.compile(r'data-([a-z0-9\-]+)="([^"]*)"', re.IGNORECASE)
BUILDING_LINK_PATTERN = re.compile(r'/en/hong-kong/n/\d+-[^/]+/b/(\d+)--[^/]+/transactions')
NEIGHBOURHOOD_LINK_PATTERN = re.compile(r'/en/hong-kong/n/(\d+)-[^/]+/transactions')
CSRF_PATTERN = re.compile(r'<meta name="csrf-token" content="([^"]+)"', re.IGNORECASE)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", value).strip()


def extract_column(body: str, column_name: str) -> str:
    pattern = re.compile(
        rf'__table-column--{re.escape(column_name)}"[^>]*>(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return ""
    content = clean_text(match.group(1))
    content = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", content).strip()
    return content


def parse_float(value: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_price_text(value: str) -> float | None:
    text = value.strip().upper().replace("HK$", "").replace(",", "")
    if not text or text == "-":
        return None
    multiplier = 1.0
    if text.endswith("M"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.endswith("K"):
        multiplier = 1_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def parse_area_text(value: str) -> float | None:
    text = value.strip().replace(",", "")
    if not text or text == "-":
        return None
    text = text.split("/")[0].strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_unix_date(value: str) -> str | None:
    number = parse_float(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


def fetch_html() -> str:
    request = Request(URL, headers=HEADERS)
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "ignore")


def fetch_partial(csrf_token: str, params: dict[str, str]) -> str:
    query_params = {
        "refresh_partial_type": "transaction_history",
        "authenticity_token": csrf_token,
    }
    query_params.update(params)
    partial_url = f"{URL}?{urlencode(query_params, doseq=True)}"
    request = Request(partial_url, headers=XHR_HEADERS)
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "ignore")


def extract_building_ids(html: str) -> list[str]:
    return sorted(set(BUILDING_LINK_PATTERN.findall(html)))


def extract_neighbourhood_ids(html: str) -> list[str]:
    return sorted(set(NEIGHBOURHOOD_LINK_PATTERN.findall(html)))


def parse_rows(html: str) -> list[dict]:
    rows: list[dict] = []
    for attrs, body in ROW_PATTERN.findall(html):
        attr_map = {k: v for k, v in ATTR_PATTERN.findall(attrs)}
        row = {
            "transaction_date": extract_column(body, "date"),
            "neighbourhood": extract_column(body, "neighbourhood"),
            "building_name": extract_column(body, "building"),
            "floor": extract_column(body, "floor"),
            "flat": extract_column(body, "flat"),
            "price_text": extract_column(body, "price"),
            "net_area_text": extract_column(body, "net-area"),
            "price_per_sqft_text": extract_column(body, "price-per-area"),
            "built_year_text": extract_column(body, "built-year"),
            "record_id": attr_map.get("real-estate-transaction-record-id"),
            "registered_at_unix": attr_map.get("registered-at"),
            "price_attr": attr_map.get("price"),
            "net_area_attr": attr_map.get("net-area-size"),
            "price_per_sqft_attr": attr_map.get("price-per-net-area-unit"),
            "source_url": URL,
        }

        row["registered_date_utc"] = parse_unix_date(row["registered_at_unix"])
        row["price_hkd"] = parse_float(row["price_attr"]) or parse_price_text(row["price_text"])
        row["net_area_sqft"] = parse_float(row["net_area_attr"]) or parse_area_text(row["net_area_text"])
        row["price_per_sqft"] = parse_float(row["price_per_sqft_attr"])

        built_year = row["built_year_text"].strip()
        row["built_year"] = int(built_year) if built_year.isdigit() else None

        rows.append(row)
    return rows


def collect_rows(max_neighbourhoods: int, max_buildings: int, sleep_seconds: float) -> list[dict]:
    first_page_html = fetch_html()
    csrf_match = CSRF_PATTERN.search(first_page_html)
    if not csrf_match:
        raise RuntimeError("Unable to locate CSRF token in Spacious transactions page.")

    csrf_token = csrf_match.group(1)
    all_rows = parse_rows(first_page_html)

    neighbourhood_ids = extract_neighbourhood_ids(first_page_html)[:max_neighbourhoods]
    building_ids = set(extract_building_ids(first_page_html))

    for neighbourhood_id in neighbourhood_ids:
        partial_html = fetch_partial(csrf_token, {"transaction_filter[neighbourhood_id]": neighbourhood_id})
        all_rows.extend(parse_rows(partial_html))
        building_ids.update(extract_building_ids(partial_html))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    for building_id in sorted(building_ids)[:max_buildings]:
        partial_html = fetch_partial(csrf_token, {"transaction_filter[building_id]": building_id})
        all_rows.extend(parse_rows(partial_html))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return all_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Spacious HK transactions with partial-history expansion.")
    parser.add_argument("--max-neighbourhoods", type=int, default=30)
    parser.add_argument("--max-buildings", type=int, default=300)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = collect_rows(
        max_neighbourhoods=max(args.max_neighbourhoods, 0),
        max_buildings=max(args.max_buildings, 0),
        sleep_seconds=max(args.sleep_seconds, 0.0),
    )

    if not rows:
        raise RuntimeError("No transaction rows were parsed from Spacious HTML.")

    dataframe = pd.DataFrame(rows)
    dataframe = dataframe.drop_duplicates(subset=["record_id"], keep="first")
    dataframe = dataframe.sort_values(["transaction_date", "record_id"], ascending=[False, False], na_position="last")

    dataframe.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(dataframe):,} rows to {OUTPUT_FILE}")
    print("Columns:", len(dataframe.columns))
    print("Date range:", dataframe["transaction_date"].dropna().min(), "to", dataframe["transaction_date"].dropna().max())
    print("Sample:")
    print(dataframe[["transaction_date", "neighbourhood", "building_name", "price_hkd", "net_area_sqft", "price_per_sqft"]].head(5).to_string(index=False))


if __name__ == "__main__":
    main()