from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pandas as pd


BASE_URL = "https://www.midland.com.hk"
START_URL = "https://www.midland.com.hk/en/transaction-history/list"
OUTPUT_DIR = Path("data_outputs")
OUTPUT_FILE = OUTPUT_DIR / "midland_transactions_history.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.midland.com.hk/",
}

ROUTE_PATTERN = re.compile(r"/en/transaction-history/[A-Za-z0-9%\-_/]+", re.IGNORECASE)
ESTATE_ID_PATTERN = re.compile(r"-E\d+$")


@dataclass
class LimitDiagnostics:
    total_requests: int = 0
    http_429_count: int = 0
    http_403_count: int = 0
    repeated_page_count: int = 0


def fetch_html(url: str, diagnostics: LimitDiagnostics) -> str:
    diagnostics.total_requests += 1
    request = Request(url, headers=HEADERS)
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", "ignore")
    except HTTPError as error:
        if error.code == 429:
            diagnostics.http_429_count += 1
        if error.code == 403:
            diagnostics.http_403_count += 1
        raise


def parse_records(html: str, source_url: str) -> list[dict]:
    raw_links = ROUTE_PATTERN.findall(html)
    links: list[str] = []
    for relative_link in sorted(set(raw_links)):
        if "/_next/" in relative_link:
            continue
        if relative_link.endswith("/undefined"):
            continue
        if relative_link == "/en/transaction-history/list":
            continue
        if relative_link.startswith("/en/transaction-history/list/"):
            continue
        if not ESTATE_ID_PATTERN.search(relative_link):
            continue
        links.append(relative_link)

    rows: list[dict] = []
    for relative_link in links:
        absolute_link = urljoin(BASE_URL, relative_link)
        slug = relative_link.rsplit("/", 1)[-1]
        rows.append(
            {
                "source": "midland",
                "transaction_detail_url": absolute_link,
                "slug": slug,
                "source_list_page": source_url,
            }
        )
    return rows


def run_crawl(max_pages: int, sleep_seconds: float) -> tuple[pd.DataFrame, LimitDiagnostics]:
    diagnostics = LimitDiagnostics()
    visited_pages: set[str] = set([START_URL])
    collected_rows: list[dict] = []

    seed_html = fetch_html(START_URL, diagnostics)
    seed_rows = parse_records(seed_html, START_URL)
    collected_rows.extend(seed_rows)

    candidate_urls = [row["transaction_detail_url"] for row in seed_rows]

    page_index = 0
    for page_url in candidate_urls:
        if page_index >= max_pages:
            break
        if page_url in visited_pages:
            continue

        try:
            detail_html = fetch_html(page_url, diagnostics)
        except HTTPError:
            continue

        visited_pages.add(page_url)
        page_index += 1

        rows = parse_records(detail_html, page_url)
        if not rows:
            diagnostics.repeated_page_count += 1
        collected_rows.extend(rows)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    dataframe = pd.DataFrame(collected_rows)
    if not dataframe.empty:
        dataframe = dataframe.drop_duplicates(subset=["transaction_detail_url"])

    return dataframe, diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Midland transaction history links and detect limit signals.")
    parser.add_argument("--max-pages", type=int, default=300)
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    dataframe, diagnostics = run_crawl(max_pages=max(args.max_pages, 1), sleep_seconds=max(args.sleep_seconds, 0.0))
    dataframe.to_csv(OUTPUT_FILE, index=False)

    print(f"Saved {len(dataframe):,} rows to {OUTPUT_FILE}")
    print("Diagnostics:")
    print(" total_requests:", diagnostics.total_requests)
    print(" http_429_count:", diagnostics.http_429_count)
    print(" http_403_count:", diagnostics.http_403_count)
    print(" repeated_page_count:", diagnostics.repeated_page_count)


if __name__ == "__main__":
    main()
