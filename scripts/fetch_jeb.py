#!/usr/bin/env python3
"""
Fetch recent/complete works from Journal of Experimental Biology (JEB)
via Crossref and write a compact JSON file for the website.

Features:
- Validates and requires a contact email (via env or settings.json)
- Sets a polite User-Agent including the contact email
- Uses Crossref cursor pagination to retrieve all records
- Retries transient network errors with exponential backoff
- Avoids logging the raw email address
- Writes results to 'jeb.json' by default (override with --out)

Usage (GitHub Actions friendly):
  env CROSSREF_MAILTO=ciaran.shaughnessy@okstate.edu python scripts/fetch_jeb.py --out jeb.json

Optional settings file (repo root):
  settings.json
  {
    "crossref_mailto": "ciaran.shaughnessy@okstate.edu",
    "crossref_user_agent_prefix": "shaughnessylab-website/1.0"
  }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from backoff import on_exception, expo


# -------------------------
# Config
# -------------------------

CROSSREF_API_URL = "https://api.crossref.org/works"
# Journal of Experimental Biology print + electronic ISSNs:
JEB_ISSNS = ["0022-0949", "1477-9145"]

# We limit to the fields we actually need to keep payloads smaller.
CROSSREF_SELECT = ",".join(
    [
        "DOI",
        "title",
        "author",
        "issued",
        "type",
        "URL",
        "volume",
        "issue",
        "page",
        "container-title",
    ]
)

# How many records per page (Crossref allows up to 1000)
ROWS = 1000


# -------------------------
# Utilities
# -------------------------

def _valid_email(s: str) -> bool:
    """Basic sanity check for an email string."""
    return bool(s and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))


def _load_settings(path: str = "settings.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Warning: could not read {path}: {e}", file=sys.stderr)
        return {}


def _get_mailto(settings: Dict[str, Any]) -> str:
    """
    Prefer env var CROSSREF_MAILTO (so we never commit emails),
    else fallback to settings['crossref_mailto'].
    """
    env_email = (os.getenv("CROSSREF_MAILTO") or "").strip()
    cfg_email = (settings.get("crossref_mailto") or "").strip()
    mailto = env_email or cfg_email
    if not _valid_email(mailto):
        raise SystemExit(
            "Crossref requires a valid contact email.\n"
            "Set environment variable CROSSREF_MAILTO or define 'crossref_mailto' in settings.json."
        )
    return mailto


def _get_user_agent(settings: Dict[str, Any], mailto: str) -> str:
    """
    Build a polite User-Agent string, optionally prefixed by settings.
    Example: shaughnessylab-website/1.0 (+mailto:you@uni.edu)
    """
    prefix = (settings.get("crossref_user_agent_prefix") or "shaughnessylab-website/1.0").strip()
    return f"{prefix} (+mailto:{mailto})"


SESSION = requests.Session()


@on_exception(expo, (requests.exceptions.RequestException,), max_time=90)
def _http_get(url: str, params: Dict[str, Any], headers: Dict[str, str]) -> requests.Response:
    """
    GET with retries on transient network errors.
    If Crossref returns 400, we raise immediately with response text to make debugging easier.
    """
    # Safe logging (do not leak the email)
    safe_params = dict(params)
    if "mailto" in safe_params:
        safe_params["mailto"] = "[redacted]"
    print(f"GET {url} {safe_params}")

    r = SESSION.get(url, params=params, headers=headers, timeout=45)
    if r.status_code == 400:
        # Bubble up useful error details without retrying
        raise requests.HTTPError(f"400 from Crossref: {r.text}", response=r)
    r.raise_for_status()
    return r


def _normalize_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a Crossref 'work' to a compact structure for the site.
    Keep this stable so the site JSON consumer remains happy.
    """
    # Titles can be lists in Crossref
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

    # 'issued' is typically like {"date-parts": [[YYYY, MM, DD]]}
    issued_year = None
    issued_parts = item.get("issued", {}).get("date-parts", [])
    if issued_parts and isinstance(issued_parts[0], list) and issued_parts[0]:
        issued_year = issued_parts[0][0]

    authors = []
    for a in item.get("author", []) or []:
        given = a.get("given") or ""
        family = a.get("family") or ""
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


def fetch_crossref_all(mailto: str, user_agent: str) -> List[Dict[str, Any]]:
    """
    Fetch all JEB records using Crossref cursor pagination.
    Returns a list of normalized records.
    """
    headers = {"User-Agent": user_agent}
    cursor = "*"
    results: List[Dict[str, Any]] = []
    page = 0

    # Crossref allows multiple filters separated by commas (logical OR).
    filter_str = f"issn:{JEB_ISSNS[0]},{JEB_ISSNS[1]}"

    while True:
        page += 1
        params = {
            "filter": filter_str,
            "rows": ROWS,
            "cursor": cursor,
            "select": CROSSREF_SELECT,
            "mailto": mailto,
            # Sorting can be added if you need deterministic page ordering, but it's not
            # required for cursor-based pagination.
            # "sort": "issued",
            # "order": "desc",
        }

        resp = _http_get(CROSSREF_API_URL, params=params, headers=headers)
        data = resp.json()

        message = data.get("message", {})
        items = message.get("items", []) or []
        next_cursor = message.get("next-cursor")

        # Normalize and append
        for it in items:
            results.append(_normalize_record(it))

        print(f"Page {page}: fetched {len(items)} items; total so far: {len(results)}")

        # Stop when no more items or cursor isn't advancing
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return results


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {path} ({len(payload) if isinstance(payload, list) else 'object'} records)")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch JEB records from Crossref and write jeb.json.")
    p.add_argument("--out", default="jeb.json", help="Output JSON path (default: jeb.json)")
    p.add_argument(
        "--settings",
        default="settings.json",
        help="Path to settings.json (default: settings.json)",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    settings = _load_settings(args.settings)
    mailto = _get_mailto(settings)
    user_agent = _get_user_agent(settings, mailto)

    print("Using User-Agent:", _get_user_agent(settings, "[redacted]"))
    print("Fetching Crossref records for JEB…")

    try:
        records = fetch_crossref_all(mailto=mailto, user_agent=user_agent)
    except requests.HTTPError as e:
        # Surface Crossref error body for 400s and similar
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1

    # Optional: sort by year desc, then title
    records.sort(key=lambda r: (r.get("year") or 0, r.get("title") or ""), reverse=True)

    write_json(args.out, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())