# ===============================
# File: scripts/fetch_jeb.py
# Purpose: Build /data/jeb.json and /data/jeb-meta.json by querying Crossref (+ Unpaywall, PubMed, OpenAlex)
# ===============================

import os, sys, json, time, re
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
import requests
import backoff

DATA_DIR = os.path.join("data")
OUT_JSON = os.path.join(DATA_DIR, "jeb.json")
META_JSON = os.path.join(DATA_DIR, "jeb-meta.json")
CACHE_JSON = os.path.join(DATA_DIR, "jeb-cache.json")  # DOI -> enriched record

ISSNS = ["0022-0949", "1477-9145"]  # JEB print & online
CROSSREF_MAILTO = os.getenv("MAILTO", "your_email@okstate.edu")
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "your_email@okstate.edu")
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "your_email@okstate.edu")

# Re-enrich recent items each run
FULL_REFRESH_YEARS = 2

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- helpers ----------
def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


@backoff.on_exception(backoff.expo, (requests.RequestException,), max_tries=5)
def http_get(url, params=None, headers=None):
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    r = requests.get(
        url,
        headers=headers or {"User-Agent": f"JEB-Explorer/1.0 (mailto:{CROSSREF_MAILTO})"},
        timeout=30,
    )
    r.raise_for_status()
    return r


# ---------- Crossref (primary) ----------
def fetch_crossref_all():
    """Fetch all JEB records via Crossref using cursor pagination."""
    base = "https://api.crossref.org/works"
    filt = f"issn:{','.join(ISSNS)}"
    rows = 1000
    cursor = "*"
    items = []
    total = None

    while True:
        params = {
            "filter": filt,
            "rows": rows,
            "mailto": CROSSREF_MAILTO,
            "cursor": cursor,
            "select": "DOI,title,author,issued,type,URL,volume,issue,page,container-title",
        }
        r = http_get(base, params=params).json()
        if total is None:
            total = r.get("message", {}).get("total-results")
            print(f"Crossref total-results: {total}")
        chunk = r.get("message", {}).get("items", []) or []
        items.extend(chunk)
        next_cursor = r.get("message", {}).get("next-cursor")
        print(f"Fetched {len(items)} / {total} ...")
        if not chunk or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(1)

    # normalize
    out = []
    for it in items:
        doi = it.get("DOI")
        year = None
        issued = it.get("issued", {})
        if "date-parts" in issued and issued["date-parts"]:
            year = issued["date-parts"][0][0]
        title = (it.get("title") or [""])[0]
        journal = (it.get("container-title") or ["Journal of Experimental Biology"])[0]
        authors = []
        for a in it.get("author", []) or []:
            name = ", ".join(filter(None, [a.get("family"), a.get("given")]))
            if name:
                authors.append(name)
        out.append(
            {
                "doi": doi,
                "title": title,
                "published_year": year,
                "journal": journal,
                "type": it.get("type"),
                "authors": authors,
                "url": it.get("URL"),
                "volume": it.get("volume"),
                "issue": it.get("issue"),
                "pages": it.get("page"),
            }
        )
    return out


# ---------- Unpaywall (OA) ----------
def enrich_unpaywall(rec):
    doi = rec.get("doi")
    if not doi:
        return rec
    url = f"https://api.unpaywall.org/v2/{quote(doi)}"
    try:
        data = http_get(url, params={"email": UNPAYWALL_EMAIL}).json()
        rec["oa"] = {
            "is_oa": bool(data.get("is_oa")),
            "license": data.get("license"),
            "oa_url": (data.get("best_oa_location") or {}).get("url"),
        }
    except Exception:
        rec["oa"] = {"is_oa": False}
    time.sleep(0.15)
    return rec


# ---------- PubMed / MeSH ----------
def doi_to_pmid(doi):
    term = f"{doi}[DOI]"
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {"db": "pubmed", "term": term, "retmode": "json"}
    try:
        js = http_get(url, params=params).json()
        ids = js.get("esearchresult", {}).get("idlist", []) or []
        return ids[0] if ids else None
    except Exception:
        return None


def fetch_pubmed_summary(pmid):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    try:
        xml = http_get(url, params=params).text
        abstract = " ".join(re.findall(r"<AbstractText.*?>(.*?)</AbstractText>", xml, flags=re.S))
        mesh = re.findall(r"<DescriptorName[^>]*?>(.*?)</DescriptorName>", xml)
        mesh = sorted(list({m.strip() for m in mesh if m.strip()}))[:50]
        return abstract.strip(), mesh
    except Exception:
        return None, []


# ---------- OpenAlex (citations) ----------
def enrich_openalex(rec):
    doi = rec.get("doi")
    if not doi:
        return rec
    url = f"https://api.openalex.org/works/doi:{quote(doi)}"
    try:
        js = http_get(url, params={"mailto": OPENALEX_MAILTO}).json()
        rec["citations"] = {"count": js.get("cited_by_count")}
    except Exception:
        pass
    time.sleep(0.15)
    return rec


# ---------- Build pipeline ----------
def main():
    cache = load_json(CACHE_JSON, {})

    # 1) fetch all base metadata
    base = fetch_crossref_all()

    # 2) decide which records need full refresh
    this_year = datetime.now(timezone.utc).year
    must_refresh = {
        r["doi"]
        for r in base
        if r.get("published_year") and r["published_year"] >= this_year - FULL_REFRESH_YEARS
    }

    results = []
    for i, rec in enumerate(base, 1):
        doi = rec.get("doi")
        cached = cache.get(doi)
        use_cache = cached and doi not in must_refresh
        if use_cache:
            results.append(cached)
            continue

        # Enrich OA
        rec = enrich_unpaywall(rec)

        # Link PubMed + MeSH
        pmid = doi_to_pmid(doi)
        if pmid:
            rec.setdefault("ids", {})["pmid"] = pmid
            abstract, mesh = fetch_pubmed_summary(pmid)
            if abstract:
                rec["abstract"] = abstract
            if mesh:
                rec["mesh"] = mesh

        # Citations (optional)
        rec = enrich_openalex(rec)

        cache[doi] = rec
        results.append(rec)
        if i % 50 == 0:
            print(f"Enriched {i}/{len(base)}")

    # 3) Save outputs
    results.sort(
        key=lambda r: (r.get("published_year") or 0, r.get("title") or ""),
        reverse=True,
    )
    payload = {
        "journal": "Journal of Experimental Biology",
        "issn": ISSNS,
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(results),
        "items": results,
    }

    save_json(OUT_JSON, payload)
    save_json(META_JSON, {"last_updated": payload["last_updated"], "count": payload["count"]})
    save_json(CACHE_JSON, cache)
    print(f"Wrote {OUT_JSON} with {len(results)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())