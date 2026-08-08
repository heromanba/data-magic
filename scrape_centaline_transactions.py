from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd


API_URL = "https://hk.centanet.com/findproperty/api/Transaction/Search"
OUTPUT_DIR = Path("data_outputs")
OUTPUT_FILE = OUTPUT_DIR / "centaline_transactions_api.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://hk.centanet.com",
    "Referer": "https://hk.centanet.com/findproperty/list/transaction",
}


@dataclass
class LimitDiagnostics:
    total_requests: int = 0
    http_429_count: int = 0
    http_403_count: int = 0
    http_500_count: int = 0
    max_size_ok: int = 0
    first_repeated_offset: int | None = None


def post_search(payload: dict, diagnostics: LimitDiagnostics) -> dict:
    diagnostics.total_requests += 1
    request = Request(API_URL, data=json.dumps(payload).encode("utf-8"), headers=HEADERS, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except HTTPError as error:
        if error.code == 429:
            diagnostics.http_429_count += 1
        elif error.code == 403:
            diagnostics.http_403_count += 1
        elif error.code == 500:
            diagnostics.http_500_count += 1
        raise


def choose_safe_size(diagnostics: LimitDiagnostics) -> int:
    tested_sizes = [200, 150, 120, 100, 80, 50, 24]
    base_payload = {
        "postType": "Both",
        "day": "Day1095",
        "sort": "InsOrRegDate",
        "order": "Descending",
        "offset": 0,
        "pageSource": "list",
    }

    for size in tested_sizes:
        payload = dict(base_payload)
        payload["size"] = size
        try:
            response = post_search(payload, diagnostics)
        except HTTPError:
            continue

        rows = response.get("data") or []
        if len(rows) > 0:
            diagnostics.max_size_ok = size
            return size

    diagnostics.max_size_ok = 24
    return 24


def normalize_row(row: dict, offset: int) -> dict:
    scope = row.get("scope") or {}
    special_case = row.get("specialCase") or {}
    bldg_grp = row.get("bldgGrp") or {}

    return {
        "source": "centaline",
        "id": row.get("id"),
        "transaction_detail_url": row.get("detailUrl"),
        "district_name": row.get("districtName"),
        "estate_name": row.get("estateName"),
        "building_name": row.get("buildingName"),
        "address": row.get("address"),
        "floor": row.get("yAxis"),
        "unit": row.get("xAxis"),
        "post_type": row.get("postType"),
        "transaction_price": row.get("transactionPrice"),
        "gross_area_sqft": row.get("gArea"),
        "gross_unit_price": row.get("gUnitPrice"),
        "net_area_sqft": row.get("nArea"),
        "net_unit_price": row.get("nUnitPrice"),
        "ins_date": row.get("insDate"),
        "direction": row.get("direction"),
        "bedroom_count": row.get("bedroomCount"),
        "data_source": row.get("dataSource"),
        "estate_type": row.get("estateType"),
        "unit_type": row.get("unitType"),
        "special_case": special_case.get("value") if isinstance(special_case, dict) else None,
        "scope_market": scope.get("scp_mkt") if isinstance(scope, dict) else None,
        "scope_territory": scope.get("terr") if isinstance(scope, dict) else None,
        "scope_district": scope.get("db") if isinstance(scope, dict) else None,
        "scope_hma": scope.get("hma") if isinstance(scope, dict) else None,
        "building_group_id": bldg_grp.get("bldgGrpId") if isinstance(bldg_grp, dict) else None,
        "building_group_name": bldg_grp.get("bldgGrpName") if isinstance(bldg_grp, dict) else None,
        "offset": offset,
    }


def run_crawl(max_pages: int, sleep_seconds: float) -> tuple[pd.DataFrame, LimitDiagnostics, int]:
    diagnostics = LimitDiagnostics()
    page_size = choose_safe_size(diagnostics)

    base_payload = {
        "postType": "Both",
        "day": "Day1095",
        "sort": "InsOrRegDate",
        "order": "Descending",
        "size": page_size,
        "pageSource": "list",
    }

    all_rows: list[dict] = []
    seen_ids: set[str] = set()

    first_response = post_search({**base_payload, "offset": 0}, diagnostics)
    total_count = int(first_response.get("count") or 0)

    first_data = first_response.get("data") or []
    for row in first_data:
        normalized = normalize_row(row, 0)
        row_id = normalized.get("id")
        if row_id and row_id not in seen_ids:
            seen_ids.add(row_id)
            all_rows.append(normalized)

    for page_index in range(1, max_pages):
        offset = page_index * page_size
        if total_count and offset >= total_count:
            break

        try:
            response = post_search({**base_payload, "offset": offset}, diagnostics)
        except HTTPError:
            break

        data = response.get("data") or []
        if not data:
            break

        new_count = 0
        for row in data:
            normalized = normalize_row(row, offset)
            row_id = normalized.get("id")
            if row_id and row_id not in seen_ids:
                seen_ids.add(row_id)
                all_rows.append(normalized)
                new_count += 1

        if new_count == 0:
            diagnostics.first_repeated_offset = offset
            break

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    dataframe = pd.DataFrame(all_rows)
    if not dataframe.empty:
        dataframe = dataframe.drop_duplicates(subset=["id"], keep="first")

    return dataframe, diagnostics, total_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Centaline transactions via public API and detect practical limits.")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    dataframe, diagnostics, total_count = run_crawl(max_pages=max(args.max_pages, 1), sleep_seconds=max(args.sleep_seconds, 0.0))
    dataframe.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(dataframe):,} rows to {OUTPUT_FILE}")
    print("API total_count field:", total_count)
    print("Diagnostics:")
    print(" total_requests:", diagnostics.total_requests)
    print(" http_429_count:", diagnostics.http_429_count)
    print(" http_403_count:", diagnostics.http_403_count)
    print(" http_500_count:", diagnostics.http_500_count)
    print(" max_size_ok:", diagnostics.max_size_ok)
    print(" first_repeated_offset:", diagnostics.first_repeated_offset)


if __name__ == "__main__":
    main()
