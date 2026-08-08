from __future__ import annotations

import argparse
import json
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


SEARCH_URL = "https://hk.centanet.com/findproperty/api/Transaction/Search"
FILTER_OPTIONS_URL = "https://hk.centanet.com/findproperty/api/Transaction/GetTransactionFilterOptions"
AUTOCOMPLETE_URL = "https://hk.centanet.com/findproperty/api/Transaction/GetTransactionAutoComplete"

OUTPUT_DIR = Path("data_outputs")
OUTPUT_FILE = OUTPUT_DIR / "centaline_transactions_api.csv"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://hk.centanet.com",
    "Referer": "https://hk.centanet.com/findproperty/en/list/transaction",
    "Platform": "Web",
}

MAX_ROWS_PER_QUERY = 10_000
SPLIT_KEYS = (
    "typeCodes",
    "bigestAndEstate",
    "phaseAndEstate",
    "developers",
    "mtrs",
)


@dataclass
class LimitDiagnostics:
    total_requests: int = 0
    http_429_count: int = 0
    http_403_count: int = 0
    http_500_count: int = 0
    network_error_count: int = 0
    max_size_ok: int = 0
    segment_queries_attempted: int = 0
    segment_queries_with_data: int = 0
    hard_cap_offsets: list[int] = field(default_factory=list)


def post_json(
    url: str,
    payload: dict[str, Any],
    diagnostics: LimitDiagnostics,
    query_params: dict[str, str] | None = None,
    max_retries: int = 6,
    retry_backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    request_url = url
    if query_params:
        request_url = f"{url}?{urlencode(query_params)}"

    attempt = 0
    while True:
        diagnostics.total_requests += 1
        request = Request(request_url, data=json.dumps(payload).encode("utf-8"), headers=REQUEST_HEADERS, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except HTTPError as error:
            if error.code == 429:
                diagnostics.http_429_count += 1
                if attempt >= max_retries:
                    raise
                sleep_seconds = retry_backoff_seconds * (2 ** attempt)
                time.sleep(max(0.0, sleep_seconds))
                attempt += 1
                continue
            if error.code == 403:
                diagnostics.http_403_count += 1
            elif error.code == 500:
                diagnostics.http_500_count += 1
            raise
        except URLError:
            diagnostics.network_error_count += 1
            if attempt >= max_retries:
                raise
            sleep_seconds = retry_backoff_seconds * (2 ** attempt)
            time.sleep(max(0.0, sleep_seconds))
            attempt += 1
            continue


def choose_safe_size(base_search: dict[str, Any], diagnostics: LimitDiagnostics) -> int:
    for size in (200, 150, 120, 100, 80, 50, 24):
        payload = dict(base_search)
        payload["size"] = size
        payload["offset"] = 0
        payload["pageSource"] = "list"
        try:
            result = post_json(SEARCH_URL, payload, diagnostics)
        except HTTPError:
            continue
        if len(result.get("data") or []) > 0:
            diagnostics.max_size_ok = size
            return size
    diagnostics.max_size_ok = 24
    return 24


def normalize_row(row: dict[str, Any], query_name: str, offset: int) -> dict[str, Any]:
    scope = row.get("scope") or {}
    special_case = row.get("specialCase") or {}
    bldg_grp = row.get("bldgGrp") or {}

    return {
        "source": "centaline",
        "query_name": query_name,
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


def signature_for_search(search: dict[str, Any]) -> str:
    keys = sorted(search.keys())
    normalized: dict[str, Any] = {}
    for key in keys:
        value = search[key]
        if isinstance(value, list):
            normalized[key] = sorted(str(v) for v in value)
        else:
            normalized[key] = value
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


def get_count(search: dict[str, Any], diagnostics: LimitDiagnostics) -> int:
    payload = dict(search)
    payload["size"] = 1
    payload["offset"] = 0
    payload["pageSource"] = "list"
    try:
        result = post_json(SEARCH_URL, payload, diagnostics)
    except Exception:
        return 0
    return int(result.get("count") or 0)


def crawl_single_query(
    search: dict[str, Any],
    query_name: str,
    page_size: int,
    max_pages_per_segment: int,
    sleep_seconds: float,
    diagnostics: LimitDiagnostics,
) -> tuple[list[dict[str, Any]], int]:
    count = get_count(search, diagnostics)
    if count <= 0:
        return [], 0

    cap_count = min(count, MAX_ROWS_PER_QUERY)
    rows: list[dict[str, Any]] = []
    pages = min(max_pages_per_segment, (cap_count + page_size - 1) // page_size)

    for page_index in range(pages):
        offset = page_index * page_size
        payload = dict(search)
        payload["size"] = page_size
        payload["offset"] = offset
        payload["pageSource"] = "list"
        try:
            result = post_json(SEARCH_URL, payload, diagnostics)
        except Exception as error:
            if isinstance(error, HTTPError) and error.code == 500 and offset >= MAX_ROWS_PER_QUERY:
                diagnostics.hard_cap_offsets.append(offset)
            break

        data_rows = result.get("data") or []
        if not data_rows:
            break

        for row in data_rows:
            rows.append(normalize_row(row, query_name=query_name, offset=offset))

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return rows, count


def autocomplete_candidates(base_search: dict[str, Any], keywords: list[str], diagnostics: LimitDiagnostics) -> dict[str, set[str]]:
    discovered: dict[str, set[str]] = {key: set() for key in SPLIT_KEYS}

    for keyword in keywords:
        payload = {"keyword": keyword}
        try:
            result = post_json(AUTOCOMPLETE_URL, payload, diagnostics, query_params={"resultSet": "WithNewProp"})
        except HTTPError:
            continue

        if not isinstance(result, dict):
            continue

        for list_value in result.values():
            if not isinstance(list_value, list):
                continue
            for item in list_value:
                if not isinstance(item, dict):
                    continue
                search_payload = item.get("search")
                if not isinstance(search_payload, dict):
                    continue
                for split_key in SPLIT_KEYS:
                    value = search_payload.get(split_key)
                    if isinstance(value, list):
                        discovered[split_key].update(str(v) for v in value if v)

    # Also include top options from filter-options for quick partition coverage.
    try:
        options = post_json(FILTER_OPTIONS_URL, dict(base_search), diagnostics)
    except HTTPError:
        options = {}

    for option in options.get("estates") or []:
        if isinstance(option, dict) and option.get("value"):
            discovered["bigestAndEstate"].add(str(option["value"]))

    for option in options.get("developer") or []:
        if isinstance(option, dict) and option.get("value"):
            discovered["developers"].add(str(option["value"]))

    return discovered


def build_segment_searches(base_search: dict[str, Any], diagnostics: LimitDiagnostics, keyword_limit: int) -> list[tuple[str, dict[str, Any]]]:
    keywords = list(string.ascii_lowercase + string.digits)
    keywords += ["港", "九", "新", "屯", "荃", "沙", "將", "馬", "元", "天", "大", "中", "北", "南", "東", "西", "灣", "城"]
    keywords = keywords[: max(1, keyword_limit)]

    discovered = autocomplete_candidates(base_search, keywords, diagnostics)
    searches: list[tuple[str, dict[str, Any]]] = []
    seen_signatures: set[str] = set()

    # Explicit top-level split.
    for post_type in ("Sale", "Rent"):
        search = dict(base_search)
        search["postType"] = post_type
        sig = signature_for_search(search)
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            searches.append((f"postType={post_type}", search))

    for split_key in SPLIT_KEYS:
        for value in sorted(discovered.get(split_key, set())):
            search = dict(base_search)
            search[split_key] = [value]
            sig = signature_for_search(search)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            searches.append((f"{split_key}={value}", search))

    return searches


def run_crawl(max_segments: int, max_pages_per_segment: int, sleep_seconds: float, keyword_limit: int) -> tuple[pd.DataFrame, LimitDiagnostics, int, int]:
    diagnostics = LimitDiagnostics()
    base_search = {
        "postType": "Both",
        "day": "Day1095",
        "sort": "InsOrRegDate",
        "order": "Descending",
    }

    page_size = choose_safe_size(base_search, diagnostics)

    all_rows: list[dict[str, Any]] = []
    global_rows, global_count = crawl_single_query(
        search=base_search,
        query_name="global",
        page_size=page_size,
        max_pages_per_segment=max_pages_per_segment,
        sleep_seconds=sleep_seconds,
        diagnostics=diagnostics,
    )
    all_rows.extend(global_rows)

    segment_searches = build_segment_searches(base_search, diagnostics, keyword_limit=keyword_limit)
    for query_name, search in segment_searches[: max_segments]:
        diagnostics.segment_queries_attempted += 1
        rows, count = crawl_single_query(
            search=search,
            query_name=query_name,
            page_size=page_size,
            max_pages_per_segment=max_pages_per_segment,
            sleep_seconds=sleep_seconds,
            diagnostics=diagnostics,
        )
        if count > 0:
            diagnostics.segment_queries_with_data += 1
        all_rows.extend(rows)

    dataframe = pd.DataFrame(all_rows)
    if not dataframe.empty:
        dataframe = dataframe.drop_duplicates(subset=["id"], keep="first")

    return dataframe, diagnostics, global_count, len(segment_searches)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Centaline transactions via segmented public API queries.")
    parser.add_argument("--max-segments", type=int, default=400)
    parser.add_argument("--max-pages-per-segment", type=int, default=100)
    parser.add_argument("--keyword-limit", type=int, default=60)
    parser.add_argument("--sleep-seconds", type=float, default=0.005)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    dataframe, diagnostics, global_count, segment_searches = run_crawl(
        max_segments=max(args.max_segments, 1),
        max_pages_per_segment=max(args.max_pages_per_segment, 1),
        sleep_seconds=max(args.sleep_seconds, 0.0),
        keyword_limit=max(args.keyword_limit, 1),
    )
    dataframe.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(dataframe):,} rows to {OUTPUT_FILE}")
    print("Global query count field:", global_count)
    print("Segment search templates discovered:", segment_searches)
    print("Diagnostics:")
    print(" total_requests:", diagnostics.total_requests)
    print(" http_429_count:", diagnostics.http_429_count)
    print(" http_403_count:", diagnostics.http_403_count)
    print(" http_500_count:", diagnostics.http_500_count)
    print(" network_error_count:", diagnostics.network_error_count)
    print(" max_size_ok:", diagnostics.max_size_ok)
    print(" segment_queries_attempted:", diagnostics.segment_queries_attempted)
    print(" segment_queries_with_data:", diagnostics.segment_queries_with_data)
    print(" hard_cap_offsets_seen:", sorted(set(diagnostics.hard_cap_offsets))[:20])


if __name__ == "__main__":
    main()