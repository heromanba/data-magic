import re
import json
import os
import asyncio
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import pandas as pd
import aiohttp

INPUT_CSV = "netflix_titles.csv"
OUTPUT_CSV = "netflix_titles_enriched.csv"
PROGRESS_FILE = ".credit_enrichment_progress.json"
MAX_CONCURRENT_REQUESTS = 32
CHECKPOINT_EVERY = 100

API_URL = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "data-magic-director-enricher/1.0 (local-script)"}

ROLE_CONFIG = {
    "director": {
        "tvmaze_roles": ["Director", "Series Director", "Episode Director"],
        "wikipedia_patterns": [
            r"\|\s*director\s*=\s*([^\n\|]+)",
            r"\|\s*directors\s*=\s*([^\n\|]+)",
            r"\|\s*director1\s*=\s*([^\n\|]+)",
            r"\|\s*directed by\s*=\s*([^\n\|]+)",
        ],
        "wikidata_props": ["P57"],
        "source_bases": {"tvmaze": 0.95, "wikipedia": 0.9, "wikidata": 0.88},
    },
    "creator": {
        "tvmaze_roles": ["Creator"],
        "wikipedia_patterns": [
            r"\|\s*creator\s*=\s*([^\n\|]+)",
            r"\|\s*creators\s*=\s*([^\n\|]+)",
        ],
        "wikidata_props": ["P170"],
        "source_bases": {"tvmaze": 0.9, "wikipedia": 0.88, "wikidata": 0.84},
    },
    "producer": {
        "tvmaze_roles": ["Producer", "Executive Producer"],
        "wikipedia_patterns": [
            r"\|\s*producer\s*=\s*([^\n\|]+)",
            r"\|\s*producers\s*=\s*([^\n\|]+)",
            r"\|\s*executive producer\s*=\s*([^\n\|]+)",
            r"\|\s*executive producers\s*=\s*([^\n\|]+)",
        ],
        "wikidata_props": ["P162"],
        "source_bases": {"tvmaze": 0.86, "wikipedia": 0.84, "wikidata": 0.8},
    },
}


# Caches
search_cache: Dict[str, List[str]] = {}
entity_claim_cache: Dict[str, List[str]] = {}
label_cache: Dict[str, str] = {}
tvmaze_search_cache: Dict[str, Dict] = {}
tvmaze_crew_cache: Dict[int, List[Dict]] = {}
wikipedia_search_cache: Dict[str, str] = {}
wikipedia_extract_cache: Dict[str, str] = {}


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


async def wikipedia_page_title(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    if title in wikipedia_search_cache:
        return wikipedia_search_cache[title]

    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": title,
        "srlimit": 5,
        "srprop": "",
    }
    data = await get_json(session, sem, WIKIPEDIA_API_URL, params=params)
    if not data:
        wikipedia_search_cache[title] = ""
        return ""

    results = data.get("query", {}).get("search", [])
    if not results:
        wikipedia_search_cache[title] = ""
        return ""

    title_norm = normalize(title)
    best = ""
    best_score = 0.0
    for item in results:
        candidate = item.get("title", "")
        if not candidate:
            continue
        score = similarity(title, candidate)
        if normalize(candidate) == title_norm:
            best = candidate
            break
        if score > best_score:
            best_score = score
            best = candidate

    wikipedia_search_cache[title] = best
    return best


async def wikipedia_wikitext(session: aiohttp.ClientSession, sem: asyncio.Semaphore, page_title: str) -> str:
    if page_title in wikipedia_extract_cache:
        return wikipedia_extract_cache[page_title]

    params = {
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": page_title,
        "redirects": 1,
    }
    data = await get_json(session, sem, WIKIPEDIA_API_URL, params=params)
    if not data:
        wikipedia_extract_cache[page_title] = ""
        return ""

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        wikipedia_extract_cache[page_title] = ""
        return ""

    page = next(iter(pages.values()))
    revisions = page.get("revisions", [])
    if not revisions:
        wikipedia_extract_cache[page_title] = ""
        return ""

    revision = revisions[0]
    text = ""
    if "slots" in revision:
        text = revision.get("slots", {}).get("main", {}).get("*", "")
    else:
        text = revision.get("*", "")

    wikipedia_extract_cache[page_title] = text
    return text


def parse_wikitext_field(wikitext: str, patterns: List[str]) -> str:
    if not wikitext:
        return ""
    for pattern in patterns:
        match = re.search(pattern, wikitext, flags=re.IGNORECASE)
        if match:
            value = match.group(1)
            value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.IGNORECASE | re.DOTALL)
            value = re.sub(r"<[^>]+>", "", value)
            value = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", value)
            value = value.replace("{{", "").replace("}}", "")
            value = value.replace("\n", " ")
            value = re.sub(r"\s+", " ", value).strip()
            value = value.strip(" ,;")
            if value:
                return value

    return ""


def parse_wikipedia_role(page_wikitext: str, role: str) -> str:
    return parse_wikitext_field(page_wikitext, ROLE_CONFIG[role]["wikipedia_patterns"])


def normalize_director_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = value.strip(" ,;:.-")
    value = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"<[^>]+>", "", value)
    return value


def is_valid_director_value(value: str) -> bool:
    if not value:
        return False
    if len(value) > 160:
        return False

    lowered = value.lower()
    blocked = [
        "director",
        "creator",
        "executive producer",
        "written by",
        "story by",
        "unknown",
        "n/a",
        "tba",
        "various",
        "multiple",
    ]
    if any(token in lowered for token in blocked):
        return False
    if re.search(r"\b(19|20)\d{2}\b", lowered):
        return False
    return True


def confidence(role: str, source: str, exact_title: bool) -> float:
    base = ROLE_CONFIG[role]["source_bases"].get(source, 0.0)
    return base + (0.02 if exact_title else 0.0)


def resolve_director_candidates(candidates: List[Dict[str, str]]) -> str:
    valid = []
    for candidate in candidates:
        value = normalize_director_value(candidate.get("value", ""))
        if is_valid_director_value(value):
            valid.append({**candidate, "value": value})

    if not valid:
        return ""

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for candidate in valid:
        grouped.setdefault(candidate["value"], []).append(candidate)

    consensus_value, consensus_group = max(
        grouped.items(),
        key=lambda item: (len(item[1]), max(c["confidence"] for c in item[1])),
    )
    if len(consensus_group) >= 2:
        return consensus_value

    ordered = sorted(valid, key=lambda c: c["confidence"], reverse=True)
    best = ordered[0]
    if best["confidence"] >= 0.9:
        if len(ordered) == 1 or ordered[1]["value"] == best["value"]:
            return best["value"]
    return ""


def source_priority_label(source: str) -> int:
    return {"tvmaze": 0, "wikipedia": 1, "wikidata": 2}.get(source, 99)


async def lookup_wikidata_credit(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    title: str,
    role: str,
    props: List[str],
) -> str:
    title_norm = normalize(title)
    candidate_ids = await search_entities(session, sem, title)
    if not candidate_ids:
        return ""

    candidate_labels = await get_labels(session, sem, candidate_ids)
    exact_first = []
    rest = []
    for cid in candidate_ids:
        label = candidate_labels.get(cid, "")
        if normalize(label) == title_norm:
            exact_first.append(cid)
        else:
            rest.append(cid)

    for cid in exact_first + rest:
        credit_ids = await get_wikidata_ids(session, sem, cid, props)
        if credit_ids:
            labels_map = await get_labels(session, sem, credit_ids)
            names = [normalize_director_value(labels_map.get(did, "")) for did in credit_ids]
            names = [n for n in names if is_valid_director_value(n)]
            if names:
                return ", ".join(dict.fromkeys(names))
    return ""


async def lookup_wikidata_director(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    return await lookup_wikidata_credit(session, sem, title, "director", ["P57"])


async def lookup_wikidata_creator(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    return await lookup_wikidata_credit(session, sem, title, "creator", ["P170"])


async def lookup_wikidata_producer(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    return await lookup_wikidata_credit(session, sem, title, "producer", ["P162"])


async def directors_from_wikipedia(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    page_title = await wikipedia_page_title(session, sem, title)
    if not page_title:
        return ""

    wikitext = await wikipedia_wikitext(session, sem, page_title)
    if not wikitext:
        return ""

    return parse_wikipedia_role(wikitext, "director")


async def creators_from_wikipedia(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    page_title = await wikipedia_page_title(session, sem, title)
    if not page_title:
        return ""

    wikitext = await wikipedia_wikitext(session, sem, page_title)
    if not wikitext:
        return ""

    return parse_wikipedia_role(wikitext, "creator")


async def producers_from_wikipedia(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    page_title = await wikipedia_page_title(session, sem, title)
    if not page_title:
        return ""

    wikitext = await wikipedia_wikitext(session, sem, page_title)
    if not wikitext:
        return ""

    return parse_wikipedia_role(wikitext, "producer")


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

    role_priority = ROLE_CONFIG["director"]["tvmaze_roles"]
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


async def creators_from_tvmaze(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    show = await tvmaze_best_match(session, sem, title)
    if not show:
        return ""

    show_id = show.get("id")
    if not isinstance(show_id, int):
        return ""

    crew = await tvmaze_crew(session, sem, show_id)
    if not crew:
        return ""

    names = []
    for member in crew:
        crew_type = str(member.get("type", "")).strip()
        person_name = str(member.get("person", {}).get("name", "")).strip()
        if crew_type == "Creator" and person_name:
            names.append(person_name)

    if names:
        return ", ".join(dict.fromkeys(names[:3]))
    return ""


async def producers_from_tvmaze(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> str:
    show = await tvmaze_best_match(session, sem, title)
    if not show:
        return ""

    show_id = show.get("id")
    if not isinstance(show_id, int):
        return ""

    crew = await tvmaze_crew(session, sem, show_id)
    if not crew:
        return ""

    names = []
    for member in crew:
        crew_type = str(member.get("type", "")).strip()
        person_name = str(member.get("person", {}).get("name", "")).strip()
        if crew_type in {"Producer", "Executive Producer"} and person_name:
            names.append(person_name)

    if names:
        return ", ".join(dict.fromkeys(names[:3]))
    return ""


async def get_wikidata_ids(session: aiohttp.ClientSession, sem: asyncio.Semaphore, entity_id: str, props: List[str]) -> List[str]:
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
    director_ids = []
    for prop in props:
        for claim in claims.get(prop, []):
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


async def lookup_title_credits(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> Dict[str, str]:
    try:
        return await find_credits_for_title(session, sem, title)
    except Exception:
        return {"director": "", "creator": "", "producer": ""}


async def find_credits_for_title(session: aiohttp.ClientSession, sem: asyncio.Semaphore, title: str) -> Dict[str, str]:
    if not isinstance(title, str) or not title.strip():
        return {"director": "", "creator": "", "producer": ""}

    title_norm = normalize(title)

    tvmaze_director_task = asyncio.create_task(directors_from_tvmaze(session, sem, title))
    wikipedia_director_task = asyncio.create_task(directors_from_wikipedia(session, sem, title))
    wikidata_director_task = asyncio.create_task(lookup_wikidata_director(session, sem, title))

    tvmaze_creator_task = asyncio.create_task(creators_from_tvmaze(session, sem, title))
    wikipedia_creator_task = asyncio.create_task(creators_from_wikipedia(session, sem, title))
    wikidata_creator_task = asyncio.create_task(lookup_wikidata_creator(session, sem, title))

    tvmaze_producer_task = asyncio.create_task(producers_from_tvmaze(session, sem, title))
    wikipedia_producer_task = asyncio.create_task(producers_from_wikipedia(session, sem, title))
    wikidata_producer_task = asyncio.create_task(lookup_wikidata_producer(session, sem, title))

    (
        tvmaze_director,
        wikipedia_director,
        wikidata_director,
        tvmaze_creator,
        wikipedia_creator,
        wikidata_creator,
        tvmaze_producer,
        wikipedia_producer,
        wikidata_producer,
    ) = await asyncio.gather(
        tvmaze_director_task,
        wikipedia_director_task,
        wikidata_director_task,
        tvmaze_creator_task,
        wikipedia_creator_task,
        wikidata_creator_task,
        tvmaze_producer_task,
        wikipedia_producer_task,
        wikidata_producer_task,
    )

    def build_candidates(role: str, values: List[tuple]) -> List[Dict[str, str]]:
        candidates = []
        for source, value in values:
            candidates.append(
                {
                    "source": source,
                    "value": value,
                    "confidence": confidence(role, source, normalize(value) == title_norm),
                }
            )
        return candidates

    director = resolve_director_candidates(
        build_candidates(
            "director",
            [
                ("tvmaze", tvmaze_director),
                ("wikipedia", wikipedia_director),
                ("wikidata", wikidata_director),
            ],
        )
    )
    creator = resolve_director_candidates(
        build_candidates(
            "creator",
            [
                ("tvmaze", tvmaze_creator),
                ("wikipedia", wikipedia_creator),
                ("wikidata", wikidata_creator),
            ],
        )
    )
    producer = resolve_director_candidates(
        build_candidates(
            "producer",
            [
                ("tvmaze", tvmaze_producer),
                ("wikipedia", wikipedia_producer),
                ("wikidata", wikidata_producer),
            ],
        )
    )

    return {"director": director, "creator": creator, "producer": producer}


async def build_credit_map(unique_titles: List[str]) -> Dict[str, Dict[str, str]]:
    title_to_credit: Dict[str, Dict[str, str]] = {}
    total = len(unique_titles)
    pending_titles = list(unique_titles)

    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            title_to_credit = json.load(f)
        pending_titles = [title for title in unique_titles if title not in title_to_credit]

    if not pending_titles:
        return title_to_credit

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout, connector=connector) as session:
        tasks = [asyncio.create_task(lookup_title_credits(session, sem, title)) for title in pending_titles]
        results = await asyncio.gather(*tasks)

        for index, (title, credits) in enumerate(zip(pending_titles, results), start=1):
            title_to_credit[title] = credits
            current = total - len(pending_titles) + index
            if current % CHECKPOINT_EVERY == 0 or current == total:
                print(f"Processed {current}/{total}")
                save_progress(title_to_credit)

    save_progress(title_to_credit)
    return title_to_credit


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

    unique_titles = df[title_col].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique()

    title_to_credit = await build_credit_map(list(unique_titles))

    df["director"] = df[title_col].astype(str).map(lambda t: title_to_credit.get(t, {}).get("director", "")).fillna("")
    df["creator"] = df[title_col].astype(str).map(lambda t: title_to_credit.get(t, {}).get("creator", "")).fillna("")
    df["producer"] = df[title_col].astype(str).map(lambda t: title_to_credit.get(t, {}).get("producer", "")).fillna("")
    df.to_csv(OUTPUT_CSV, index=False)

    director_matched = (df["director"].astype(str).str.strip() != "").sum()
    creator_matched = (df["creator"].astype(str).str.strip() != "").sum()
    producer_matched = (df["producer"].astype(str).str.strip() != "").sum()
    print(f"Done. Matched directors for {director_matched} rows, creators for {creator_matched} rows, producers for {producer_matched} rows out of {len(df)}.")


if __name__ == "__main__":
    asyncio.run(main())
