import re
import json
import os
import asyncio
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import pandas as pd
import aiohttp

INPUT_CSV = "missing_director.csv"
OUTPUT_CSV = "missing_director.csv"
PROGRESS_FILE = ".director_enrichment_progress.json"
MAX_CONCURRENT_REQUESTS = 32
CHECKPOINT_EVERY = 100

API_URL = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "data-magic-director-enricher/1.0 (local-script)"}


# Caches
search_cache: Dict[str, List[str]] = {}
entity_claim_cache: Dict[str, List[str]] = {}
label_cache: Dict[str, str] = {}
tvmaze_search_cache: Dict[str, Dict] = {}
tvmaze_crew_cache: Dict[int, List[Dict]] = {}


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def get_json(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    params: Optional[Dict] = None,
    timeout: int = 30,
    retries: int = 4,
):
    for attempt in range(retries):
        try:
            async with sem:
                async with session.get(url, params=params, timeout=timeout) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    if response.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def search_entities(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str, limit: int = 8) -> List[str]:
    if title in search_cache:
        return search_cache[title]

    params = {
        "action": "wbsearchentities",
        "format": "json",
        "language": "en",
        "type": "item",
        "limit": limit,
        "search": title,
    }
    data = await get_json(session, sem, API_URL, params=params)
    if not data:
        search_cache[title] = []
        return []

    ids = [item.get("id") for item in data.get("search", []) if item.get("id")]
    search_cache[title] = ids
    return ids


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


async def tvmaze_best_match(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> Dict:
    if title in tvmaze_search_cache:
        return tvmaze_search_cache[title]

    data = await get_json(session, sem, "https://api.tvmaze.com/singlesearch/shows", params={"q": title})
    if not isinstance(data, dict):
        tvmaze_search_cache[title] = {}
        return {}

    show = data
    name = show.get("name", "")
    if not name or similarity(title, name) < 0.6:
        tvmaze_search_cache[title] = {}
        return {}

    tvmaze_search_cache[title] = show
    return show


async def tvmaze_crew(session: aiohttp.ClientSession, sem: asyncio.Semaphore, show_id: int) -> List[Dict]:
    if show_id in tvmaze_crew_cache:
        return tvmaze_crew_cache[show_id]

    data = await get_json(session, sem, f"https://api.tvmaze.com/shows/{show_id}/crew")
    if not isinstance(data, list):
        tvmaze_crew_cache[show_id] = []
        return []

    crew = data
    tvmaze_crew_cache[show_id] = crew
    return crew


async def directors_from_tvmaze(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    show = await tvmaze_best_match(session, sem, title)
    if not show:
        return ""

    show_id = show.get("id")
    if not isinstance(show_id, int):
        return ""

    crew = await tvmaze_crew(session, sem, show_id)
    if not crew:
        return ""

    role_priority = ["Director", "Series Director", "Episode Director", "Creator", "Executive Producer"]
    ranked = []
    for member in crew:
        crew_type = str(member.get("type", "")).strip()
        person_name = str(member.get("person", {}).get("name", "")).strip()
        if not person_name:
            continue
        try:
            priority = role_priority.index(crew_type)
        except ValueError:
            continue
        ranked.append((priority, person_name))

    if ranked:
        ranked.sort(key=lambda item: item[0])
        names = []
        seen = set()
        for _, name in ranked:
            if name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) == 3:
                break
        return ", ".join(names)

    return ""


async def get_director_ids(session: aiohttp.ClientSession, sem: asyncio.Semaphore, entity_id: str) -> List[str]:
    if entity_id in entity_claim_cache:
        return entity_claim_cache[entity_id]

    params = {
        "action": "wbgetentities",
        "format": "json",
        "ids": entity_id,
        "props": "claims|labels",
        "languages": "en",
    }
    data = await get_json(session, sem, API_URL, params=params)
    if not data:
        entity_claim_cache[entity_id] = []
        return []

    entity = data.get("entities", {}).get(entity_id, {})

    claims = entity.get("claims", {})
    p57 = claims.get("P57", [])  # director
    p170 = claims.get("P170", [])  # creator

    director_ids = []
    for claim in p57 + p170:
        mainsnak = claim.get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        value = datavalue.get("value", {})
        if isinstance(value, dict) and value.get("id"):
            director_ids.append(value["id"])

    entity_claim_cache[entity_id] = director_ids
    return director_ids


async def get_labels(session: aiohttp.ClientSession, sem: asyncio.Semaphore, entity_ids: List[str]) -> Dict[str, str]:
    unresolved = [eid for eid in entity_ids if eid and eid not in label_cache]

    if unresolved:
        for i in range(0, len(unresolved), 50):
            batch = unresolved[i : i + 50]
            params = {
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(batch),
                "props": "labels",
                "languages": "en",
            }
            data = await get_json(session, sem, API_URL, params=params)
            if data:
                entities = data.get("entities", {})
                for eid, details in entities.items():
                    label_cache[eid] = details.get("labels", {}).get("en", {}).get("value", "")
            else:
                for eid in batch:
                    label_cache[eid] = ""

    return {eid: label_cache.get(eid, "") for eid in entity_ids}


def save_progress(title_to_director: Dict[str, str]) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(title_to_director, f, ensure_ascii=False)


async def lookup_title_director(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    try:
        return await find_directors_for_title(session, sem, title)
    except Exception:
        return ""


async def find_directors_for_title(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        return ""

    from_tvmaze = await directors_from_tvmaze(session, sem, title)
    if from_tvmaze:
        return from_tvmaze

    title_norm = normalize(title)
    candidate_ids = await search_entities(session, sem, title)

    if not candidate_ids:
        return ""

    # prioritize candidates with exact normalized label match when possible
    exact_first = []
    rest = []

    # fetch candidate labels in one batch for ranking
    candidate_labels = await get_labels(session, sem, candidate_ids)
    for cid in candidate_ids:
        label = candidate_labels.get(cid, "")
        if normalize(label) == title_norm:
            exact_first.append(cid)
        else:
            rest.append(cid)

    ordered_candidates = exact_first + rest

    for cid in ordered_candidates:
        director_ids = await get_director_ids(session, sem, cid)
        if director_ids:
            labels_map = await get_labels(session, sem, director_ids)
            names = [labels_map.get(did, "").strip() for did in director_ids]
            names = [n for n in names if n]
            if names:
                return ", ".join(dict.fromkeys(names))

    return ""


async def build_director_map(unique_titles: List[str]) -> Dict[str, str]:
    title_to_director: Dict[str, str] = {}
    total = len(unique_titles)
    pending_titles = list(unique_titles)

    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            title_to_director = json.load(f)
        pending_titles = [title for title in unique_titles if title not in title_to_director]

    if not pending_titles:
        return title_to_director

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout, connector=connector) as session:
        tasks = [asyncio.create_task(lookup_title_director(session, sem, title)) for title in pending_titles]
        results = await asyncio.gather(*tasks)

        for index, (title, director) in enumerate(zip(pending_titles, results), start=1):
            title_to_director[title] = director
            current = total - len(pending_titles) + index
            if current % CHECKPOINT_EVERY == 0 or current == total:
                print(f"Processed {current}/{total}")
                save_progress(title_to_director)

    save_progress(title_to_director)
    return title_to_director


async def main() -> None:
    df = pd.read_csv(INPUT_CSV, skipinitialspace=True)
    df.columns = [str(c).strip() for c in df.columns]

    title_col = None
    for c in df.columns:
        if c.lower() == "title":
            title_col = c
            break
    if title_col is None:
        raise ValueError("Could not find a 'title' column in missing_director.csv")

    unique_titles = (
        df[title_col].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique()
    )

    title_to_director = await build_director_map(list(unique_titles))

    df["director"] = df[title_col].astype(str).map(title_to_director).fillna("")
    df.to_csv(OUTPUT_CSV, index=False)

    matched = (df["director"].astype(str).str.strip() != "").sum()
    print(f"Done. Matched directors for {matched} of {len(df)} rows.")


if __name__ == "__main__":
    asyncio.run(main())
