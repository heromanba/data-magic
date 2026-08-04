import re
import time
from difflib import SequenceMatcher
from typing import Dict, List

import pandas as pd
import requests

INPUT_CSV = "missing_director.csv"
OUTPUT_CSV = "missing_director.csv"

API_URL = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "data-magic-director-enricher/1.0 (local-script)"}

session = requests.Session()
session.headers.update(HEADERS)


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


def search_entities(title: str, limit: int = 8) -> List[str]:
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
    r = session.get(API_URL, params=params, timeout=30)
    if r.status_code != 200:
        search_cache[title] = []
        return []

    data = r.json()
    ids = [item.get("id") for item in data.get("search", []) if item.get("id")]
    search_cache[title] = ids
    time.sleep(0.03)
    return ids


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def tvmaze_best_match(title: str) -> Dict:
    if title in tvmaze_search_cache:
        return tvmaze_search_cache[title]

    r = session.get(
        "https://api.tvmaze.com/singlesearch/shows",
        params={"q": title},
        timeout=30,
    )
    if r.status_code != 200:
        tvmaze_search_cache[title] = {}
        return {}

    show = r.json()
    name = show.get("name", "")
    if not name or similarity(title, name) < 0.6:
        tvmaze_search_cache[title] = {}
        return {}

    tvmaze_search_cache[title] = show
    time.sleep(0.03)
    return show


def tvmaze_crew(show_id: int) -> List[Dict]:
    if show_id in tvmaze_crew_cache:
        return tvmaze_crew_cache[show_id]

    r = session.get(f"https://api.tvmaze.com/shows/{show_id}/crew", timeout=30)
    if r.status_code != 200:
        tvmaze_crew_cache[show_id] = []
        return []

    crew = r.json() if isinstance(r.json(), list) else []
    tvmaze_crew_cache[show_id] = crew
    time.sleep(0.03)
    return crew


def directors_from_tvmaze(title: str) -> str:
    show = tvmaze_best_match(title)
    if not show:
        return ""

    show_id = show.get("id")
    if not isinstance(show_id, int):
        return ""

    crew = tvmaze_crew(show_id)
    if not crew:
        return ""

    role_priority = [
        "Director",
        "Series Director",
        "Episode Director",
        "Creator",
        "Executive Producer",
    ]

    for role in role_priority:
        names = []
        for member in crew:
            crew_type = str(member.get("type", "")).strip()
            person_name = str(member.get("person", {}).get("name", "")).strip()
            if crew_type == role and person_name:
                names.append(person_name)

        if names:
            unique = list(dict.fromkeys(names))
            return ", ".join(unique[:3])

    return ""


def get_director_ids(entity_id: str) -> List[str]:
    if entity_id in entity_claim_cache:
        return entity_claim_cache[entity_id]

    params = {
        "action": "wbgetentities",
        "format": "json",
        "ids": entity_id,
        "props": "claims|labels",
        "languages": "en",
    }
    r = session.get(API_URL, params=params, timeout=30)
    if r.status_code != 200:
        entity_claim_cache[entity_id] = []
        return []

    entity = r.json().get("entities", {}).get(entity_id, {})

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
    time.sleep(0.03)
    return director_ids


def get_labels(entity_ids: List[str]) -> Dict[str, str]:
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
            r = session.get(API_URL, params=params, timeout=30)
            if r.status_code == 200:
                entities = r.json().get("entities", {})
                for eid, details in entities.items():
                    label_cache[eid] = (
                        details.get("labels", {}).get("en", {}).get("value", "")
                    )
            else:
                for eid in batch:
                    label_cache[eid] = ""
            time.sleep(0.03)

    return {eid: label_cache.get(eid, "") for eid in entity_ids}


def find_directors_for_title(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        return ""

    from_tvmaze = directors_from_tvmaze(title)
    if from_tvmaze:
        return from_tvmaze

    title_norm = normalize(title)
    candidate_ids = search_entities(title)

    if not candidate_ids:
        return ""

    # prioritize candidates with exact normalized label match when possible
    exact_first = []
    rest = []

    # fetch candidate labels in one batch for ranking
    candidate_labels = get_labels(candidate_ids)
    for cid in candidate_ids:
        label = candidate_labels.get(cid, "")
        if normalize(label) == title_norm:
            exact_first.append(cid)
        else:
            rest.append(cid)

    ordered_candidates = exact_first + rest

    for cid in ordered_candidates:
        director_ids = get_director_ids(cid)
        if director_ids:
            labels_map = get_labels(director_ids)
            names = [labels_map.get(did, "").strip() for did in director_ids]
            names = [n for n in names if n]
            if names:
                return ", ".join(dict.fromkeys(names))

    return ""


def main() -> None:
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

    title_to_director: Dict[str, str] = {}
    total = len(unique_titles)
    for i, title in enumerate(unique_titles, start=1):
        director = find_directors_for_title(title)
        title_to_director[title] = director
        if i % 50 == 0 or i == total:
            print(f"Processed {i}/{total}")

    df["director"] = df[title_col].astype(str).map(title_to_director).fillna("")
    df.to_csv(OUTPUT_CSV, index=False)

    matched = (df["director"].astype(str).str.strip() != "").sum()
    print(f"Done. Matched directors for {matched} of {len(df)} rows.")


if __name__ == "__main__":
    main()
