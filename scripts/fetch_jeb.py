#!/usr/bin/env python3
# ===============================
# File: scripts/fetch_jeb.py
# Purpose: Build /data/jeb.json and /data/jeb-meta.json by querying
#          Crossref (+ Unpaywall, PubMed, OpenAlex) with caching.
# Notes:
# - Requires a top-level /data/ directory (script will create it).
# - Set repo secrets: CROSSREF_MAILTO, UNPAYWALL_EMAIL, OPENALEX_MAILTO.
# - Safe to re-run: refreshes Crossref; enrichment is cached per DOI.
# ===============================

from __future__ import annotations

import os, sys, json, time, re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import backoff

# -------------------------
# Paths & constants
# -------------------------
DATA_DIR   = os.path.join("data")
OUT_JSON   = os.path.join(DATA_DIR, "jeb.json")
META_JSON  = os.path.join(DATA_DIR, "jeb-meta.json")
CACHE_JSON = os.path.join(DATA_DIR, "jeb-cache.json")  # DOI -> enrichment

ISSNS = ["0022-0949", "1477-9145"]  # JEB print & online
ROWS  = 1000

# Environment (set via GitHub secrets)
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", "").strip()
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "").strip()
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "").strip()

if not CROSSREF_MAILTO:
    sys.exit("CROSSREF_MAILTO env var is required (set a valid email).")

# Polite, identifying UA
def user_agent(redact=False):
    m = "[redacted]" if redact else CROSSREF_MAILTO
    return f"shaughnessylab-website/1.0 (+mailto:{m})"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": user_agent()})

os.makedirs(DATA_DIR, exist_ok=True)

# -------------------------
# Utilities
# -------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _valid_email(s: str) -> bool:
    return bool(s and EMAIL_RE.match(s))

def load_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path: str, obj: Any):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def sleep_secs(s: float):
    # Gentle pacing for public APIs
    time.sleep(s)

def doi_to_path_part(doi: str) -> str:
    return doi.lower().strip()

# -------------------------
# HTTP with backoff
# -------------------------
# only retry on connection/timeout errors, not HTTP 4xx
@backoff.on_exception(backoff.expo, (requests.exceptions.ConnectionError, requests.exceptions.Timeout), max_time=90)
def http_get(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None):
    sp = dict(params or {})
    if "mailto" in sp: sp["mailto"] = "[redacted]"
    if "email"  in sp: sp["email"]  = "[redacted]"
    print("GET", url, sp)

    r = SESSION.get(url, params=params, headers=headers, timeout=45)
    r.raise_for_status()  # 4xx/5xx will raise immediately (no retry)
    return r

# -------------------------
# Normalization
# -------------------------
def normalize_crossref_item(item: Dict[str, Any]) -> Dict[str, Any]:
    title = ""
    if isinstance(item.get("title"), list) and item["title"]:
        title = item["title"][0]
    elif isinstance(item.get("title"), str):
        title = item["title"]

    container = ""
    if isinstance(item.get("container-title"), list) and item["container-title"]:
        container = item["container-title"][0]
    elif isinstance(item.get("container-title"), str):
        container = item["container-title"]

    issued_year = None
    parts = item.get("issued", {}).get("date-parts", [])
    if parts and isinstance(parts[0], list) and parts[0]:
        issued_year = parts[0][0]

    authors = []
    for a in item.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = " ".join([given, family]).strip() or (a.get("name") or "").strip()
        if name:
            authors.append(name)

    return {
        "DOI": item.get("DOI", ""),
        "title": title,
        "authors": authors,
        "year": issued_year,
        "type": item.get("type", ""),
        "URL": item.get("URL", ""),
        "volume": item.get("volume", ""),
        "issue": item.get("issue", ""),
        "page": item.get("page", ""),
        "journal": container,
    }

# -------------------------
# Crossrefs fetching
# -------------------------
def fetch_crossref_all() -> List[Dict[str, Any]]:
    url = "https://api.crossref.org/works"
    all_items: List[Dict[str, Any]] = []
    seen_dois = set()

    for issn in ISSNS:
        cursor = "*"
        page = 0
        filt = f"issn:{issn}"

        while True:
            page += 1
            params = {
                "filter": filt,
                "rows": ROWS,
                "cursor": cursor,
                "select": "DOI,title,author,issued,type,URL,volume,issue,page,container-title",
                "mailto": CROSSREF_MAILTO,
            }
            r = http_get(url, params=params, headers={"User-Agent": user_agent()})
            data = r.json().get("message", {})
            items = data.get("items", []) or []
            next_cursor = data.get("next-cursor")
            print(f"Crossref {issn} page {page}: {len(items)} items")

            for it in items:
                rec = normalize_crossref_item(it)
                doi = (rec.get("DOI") or "").lower().strip()
                if doi and doi in seen_dois:
                    continue
                if doi:
                    seen_dois.add(doi)
                all_items.append(rec)

            if not items or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            sleep_secs(0.2)

    return all_items

# -------------------------
# Unpaywall enrichment
# -------------------------
def enrich_unpaywall(doi: str) -> Dict[str, Any]:
    out = {}
    if not _valid_email(UNPAYWALL_EMAIL):
        return out
    url = f"https://api.unpaywall.org/v2/{doi}"
    r = http_get(url, params={"email": UNPAYWALL_EMAIL})
    j = r.json()
    out["unpaywall"] = {
        "oa_status": j.get("oa_status"),
        "is_oa": j.get("is_oa"),
        "license": (j.get("best_oa_location") or {}).get("license"),
        "oa_url": (j.get("best_oa_location") or {}).get("url"),
        "host_type": (j.get("best_oa_location") or {}).get("host_type"),
    }
    sleep_secs(0.15)
    return out

# -------------------------
# OpenAlex enrichment
# -------------------------
def enrich_openalex(doi: str) -> Dict[str, Any]:
    out = {}
    if not _valid_email(OPENALEX_MAILTO):
        return out
    # You can pass the DOI URL directly
    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    r = http_get(url, params={"mailto": OPENALEX_MAILTO})
    j = r.json()
    # Select a compact subset
    concepts = []
    for c in (j.get("concepts") or [])[:10]:
        concepts.append({
            "display_name": c.get("display_name"),
            "score": c.get("score"),
            "level": c.get("level"),
        })
    out["openalex"] = {
        "id": j.get("id"),
        "title": j.get("title"),
        "host_venue": (j.get("host_venue") or {}).get("display_name"),
        "publication_year": j.get("publication_year"),
        "concepts": concepts,
        "open_access": (j.get("open_access") or {}).get("is_oa"),
    }
    sleep_secs(0.15)
    return out

# -------------------------
# PubMed enrichment
# -------------------------
def enrich_pubmed(doi: str) -> Dict[str, Any]:
    """Look up PMID (and PMCID if available) by DOI via NCBI E-utilities."""
    # eSearch
    try:
        esearch = http_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"},
        ).json()
        ids = (esearch.get("esearchresult") or {}).get("idlist") or []
        if not ids:
            return {}
        pmid = ids[0]
        # eSummary for details (check pmc)
        esum = http_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": pmid, "retmode": "json"},
        ).json()
        docsum = (esum.get("result") or {}).get(pmid) or {}
        pmcid = None
        for a in docsum.get("articleids") or []:
            if a.get("idtype") == "pmcid":
                pmcid = a.get("value")
                break
        sleep_secs(0.15)
        return {"pubmed": {"pmid": pmid, "pmcid": pmcid}}
    except requests.HTTPError:
        return {}

# -------------------------
# Main
# -------------------------
def main() -> int:
    print("Using User-Agent:", user_agent(redact=True))
    print("Fetching Crossref records for JEB…")
    base_records = fetch_crossref_all()
    print(f"Crossref total: {len(base_records)}")

    # Load/prepare enrichment cache
    cache: Dict[str, Any] = load_json(CACHE_JSON, {})
    updated_cache: Dict[str, Any] = dict(cache)

    enriched_records: List[Dict[str, Any]] = []

    for i, rec in enumerate(base_records, 1):
        doi = rec.get("DOI") or ""
        if not doi:
            enriched_records.append(rec)
            continue

        cache_key = doi_to_path_part(doi)
        if cache_key in cache:
            # Use cached enrichment
            enr = cache[cache_key]
        else:
            enr = {}
            # Unpaywall
            try:
                enr.update(enrich_unpaywall(doi))
            except requests.HTTPError as e:
                print(f"Unpaywall error for {doi}: {e}")
            # OpenAlex
            try:
                enr.update(enrich_openalex(doi))
            except requests.HTTPError as e:
                print(f"OpenAlex error for {doi}: {e}")
            # PubMed
            try:
                enr.update(enrich_pubmed(doi))
            except requests.HTTPError as e:
                print(f"PubMed error for {doi}: {e}")

            updated_cache[cache_key] = enr

        # Merge enrichment into record (flat summary fields + nested blocks)
        summary = {
            "is_oa": (enr.get("unpaywall") or {}).get("is_oa"),
            "oa_status": (enr.get("unpaywall") or {}).get("oa_status"),
            "oa_url": (enr.get("unpaywall") or {}).get("oa_url"),
            "pmid": (enr.get("pubmed") or {}).get("pmid"),
            "pmcid": (enr.get("pubmed") or {}).get("pmcid"),
            "openalex_id": (enr.get("openalex") or {}).get("id"),
        }
        full = dict(rec)
        full.update(summary)
        full["enrichment"] = enr

        enriched_records.append(full)

        if i % 50 == 0:
            print(f"…enriched {i}/{len(base_records)} records")

    # Sort newest first (year desc, then title)
    enriched_records.sort(key=lambda r: (r.get("year") or 0, r.get("title") or ""), reverse=True)

    # Write outputs
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "items": [
            {
                "title": r.get("title"),
                "authors": r.get("authors") or [],
                "published_year": r.get("year"),
                "type": r.get("type"),
                "doi": r.get("DOI"),
                "url": r.get("URL") or (("https://doi.org/" + r["DOI"]) if r.get("DOI") else None),
                "abstract": r.get("abstract") or "",
                "oa": {
                    "is_oa": r.get("is_oa"),
                    "status": r.get("oa_status"),
                    "url": r.get("oa_url"),
                },
            }
            for r in enriched_records
        ],
    }
    save_json(OUT_JSON, payload)

    meta = {
        "journal": "Journal of Experimental Biology",
        "issn": ISSNS,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(enriched_records),
        "sources": {
            "crossref": True,
            "unpaywall": _valid_email(UNPAYWALL_EMAIL),
            "openalex": _valid_email(OPENALEX_MAILTO),
            "pubmed": True,
        },
    }
    save_json(META_JSON, meta)
    save_json(CACHE_JSON, updated_cache)


    print(f"Wrote {len(enriched_records)} records to {OUT_JSON}")
    print(f"Wrote meta to {META_JSON} and cache to {CACHE_JSON}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
