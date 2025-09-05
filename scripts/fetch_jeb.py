# ===============================
# File: scripts/fetch_jeb.py
# Purpose: Build /data/jeb.json and /data/jeb-meta.json by querying Crossref (+ Unpaywall, PubMed, OpenAlex)
# Notes:
# - Requires repository to have a top-level /data/ directory checked in.
# - Set repo secrets: CROSSREF_MAILTO, UNPAYWALL_EMAIL, OPENALEX_MAILTO.
# - Safe to re-run incrementally: will append new records and refresh recent years.
# ===============================


import os, sys, json, time, csv, math, re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote
import requests
import backoff


DATA_DIR = os.path.join('data')
OUT_JSON = os.path.join(DATA_DIR, 'jeb.json')
META_JSON = os.path.join(DATA_DIR, 'jeb-meta.json')
CACHE_JSON = os.path.join(DATA_DIR, 'jeb-cache.json') # maps DOI -> enriched record to avoid re-fetching


ISSNS = ['0022-0949', '1477-9145'] # JEB print & online
CROSSREF_MAILTO = os.getenv('MAILTO', 'your_email@okstate.edu')
UNPAYWALL_EMAIL = os.getenv('UNPAYWALL_EMAIL', 'your_email@okstate.edu')
OPENALEX_MAILTO = os.getenv('OPENALEX_MAILTO', 'your_email@okstate.edu')


# Years to fully refresh each run (recent issues get corrected often)
FULL_REFRESH_YEARS = 2


os.makedirs(DATA_DIR, exist_ok=True)


# ---------- helpers ----------


def save_json(path, obj):
with open(path, 'w', encoding='utf-8') as f:
json.dump(obj, f, ensure_ascii=False, indent=2)




def load_json(path, default):
if os.path.exists(path):
with open(path, 'r', encoding='utf-8') as f:
return json.load(f)
return default




@backoff.on_exception(backoff.expo, (requests.RequestException,), max_tries=5)
def http_get(url, params=None, headers=None):
if params:
url = url + ('&' if '?' in url else '?') + urlencode(params)
r = requests.get(url, headers=headers or {"User-Agent": f"JEB-Explorer/1.0 (mailto:{CROSSREF_MAILTO})"}, timeout=30)
r.raise_for_status()
return r




# ---------- Crossref (primary) ----------


def fetch_crossref_all():
"""Fetch all JEB records via Crossref using cursor pagination."""
base = 'https://api.crossref.org/works'
filt = f"issn:{','.join(ISSNS)}"
rows = 1000
cursor = '*'
items = []
total = None


while True:
params = {
'filter': filt,
'rows': rows,
'mailto': CROSSREF_MAILTO,
'cursor': cursor,
'select': 'DOI,title,author,issued,type,URL,volume,issue,page,container-title'
sys.exit(main())