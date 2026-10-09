"""GDELT DOC 2.0 client: European news by source country (Axis A) and topic (Axis B).

    python gdelt.py --probe [--hours 2]   # measure volume, 250-cap hits and 429s; writes nothing

GDELT facts this code depends on (see docs/news/EuroLens_News_Sourcing_and_Scoring.md §4
in the eurolens repo):
- sourcecountry: takes FIPS 10-4 codes, not ISO (Germany = GM, UK = UK, Greece = GR).
- The returned sourcecountry and language fields are English names ("Germany", "German").
- At most 250 articles per call and no cursor, so the only way to page is to slice time.
- One request per 5 seconds; a faster call gets HTTP 429 with a plain-text body.
- ArtList has no snippet or body: url, title, seendate, domain, language, sourcecountry.
"""
import argparse
import hashlib
import json
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter

from collect import USER_AGENT, iso, normalize_url, strip_html

API = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_INTERVAL = 6.0                 # seconds between calls; GDELT's floor is 5
MAX_RECORDS = 250
MIN_SLICE = timedelta(minutes=15)  # GDELT indexes in 15-minute updates
LAG = timedelta(minutes=15)        # the newest quarter-hour may not be indexed yet

# EuroLens ISO-2 -> GDELT FIPS 10-4 (bold-diverging ones in the spec's §4a table).
FIPS = {
    "AT": "AU", "BE": "BE", "BG": "BU", "CY": "CY", "CZ": "EZ", "DE": "GM", "DK": "DA",
    "EE": "EN", "EL": "GR", "ES": "SP", "FI": "FI", "FR": "FR", "GB": "UK", "HR": "HR",
    "HU": "HU", "IE": "EI", "IT": "IT", "LT": "LH", "LU": "LU", "LV": "LG", "MT": "MT",
    "NL": "NL", "PL": "PL", "PT": "PO", "RO": "RO", "SE": "SW", "SI": "SI", "SK": "LO",
}

# GDELT's English sourcecountry names -> EuroLens ISO-2 (Greece = EL, UK = GB).
COUNTRY_NAMES = {
    "Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Cyprus": "CY",
    "Czech Republic": "CZ", "Czechia": "CZ", "Germany": "DE", "Denmark": "DK",
    "Estonia": "EE", "Greece": "EL", "Spain": "ES", "Finland": "FI", "France": "FR",
    "United Kingdom": "GB", "Croatia": "HR", "Hungary": "HU", "Ireland": "IE",
    "Italy": "IT", "Lithuania": "LT", "Luxembourg": "LU", "Latvia": "LV", "Malta": "MT",
    "Netherlands": "NL", "Poland": "PL", "Portugal": "PT", "Romania": "RO",
    "Sweden": "SE", "Slovenia": "SI", "Slovakia": "SK",
}

# GDELT's English language names -> ISO 639-1. Unlisted languages stay null (raw kept).
LANGUAGES = {
    "English": "en", "German": "de", "French": "fr", "Spanish": "es", "Italian": "it",
    "Portuguese": "pt", "Dutch": "nl", "Polish": "pl", "Greek": "el", "Czech": "cs",
    "Slovak": "sk", "Hungarian": "hu", "Romanian": "ro", "Bulgarian": "bg",
    "Croatian": "hr", "Slovenian": "sl", "Swedish": "sv", "Danish": "da", "Finnish": "fi",
    "Estonian": "et", "Latvian": "lv", "Lithuanian": "lt", "Maltese": "mt", "Irish": "ga",
    "Catalan": "ca", "Basque": "eu", "Galician": "gl", "Luxembourgish": "lb",
    "Norwegian": "no", "Russian": "ru", "Ukrainian": "uk", "Serbian": "sr",
    "Turkish": "tr", "Arabic": "ar", "Chinese": "zh", "Japanese": "ja",
}

# Axis A: everything GDELT saw from outlets based in each country.
AXIS_A = {f"A_{cc}": f"sourcecountry:{fips}" for cc, fips in FIPS.items()}
# Axis B: Europe-affecting topics from any source country (mostly English-language
# coverage outside Europe). Tune here; capture broad, let the scoring gate decide.
AXIS_B = {
    "B_eu_institutions": '("European Union" OR "European Commission" OR "European Parliament" OR "European Council" OR Eurozone)',
    "B_security": "(NATO OR Ukraine OR Russia) (Europe OR European)",
    "B_trade": '(tariff OR tariffs OR sanctions OR "trade deal") (Europe OR European)',
    "B_energy": "(energy OR gas OR oil OR LNG) (Europe OR European)",
    "B_migration": "(migration OR migrants OR asylum OR refugees) (Europe OR European)",
}
QUERIES = {**AXIS_A, **AXIS_B}


class RateLimited(Exception):
    pass


class GdeltError(Exception):
    pass


class TLSAdapter(HTTPAdapter):
    """Explicit modern TLS context; solved a real GDELT handshake failure in the prior project."""
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = ssl.create_default_context()
        return super().init_poolmanager(*args, **kwargs)


def gdelt_time(dt):
    """
    >>> gdelt_time(datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc))
    '20261008063000'
    """
    return dt.strftime("%Y%m%d%H%M%S")


def seendate_iso(s):
    """GDELT seendate -> ISO UTC; None if absent or malformed.

    >>> seendate_iso("20261008T121500Z")
    '2026-10-08T12:15:00Z'
    >>> seendate_iso("") is None
    True
    """
    try:
        return iso(datetime.strptime(s, "%Y%m%dT%H%M%SZ"))
    except (TypeError, ValueError):
        return None


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.mount("https://", TLSAdapter())
        self.last = 0.0
        self.calls = 0
        self.rate_limited = 0

    def _wait(self):
        delay = MIN_INTERVAL - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()

    def artlist(self, query, start, end):
        """One ArtList call. Retries 429s with back-off; raises GdeltError on a query error."""
        params = {"query": query, "mode": "artlist", "maxrecords": MAX_RECORDS,
                  "sort": "datedesc", "format": "json",
                  "startdatetime": gdelt_time(start), "enddatetime": gdelt_time(end)}
        for attempt in range(4):
            self._wait()
            self.calls += 1
            try:
                resp = self.session.get(API, params=params, timeout=60,
                                        headers={"User-Agent": USER_AGENT})
            except requests.RequestException as e:
                err = f"{type(e).__name__}: {e}"
                time.sleep(10 * (attempt + 1))
                continue
            text = resp.text.strip()
            # 429 comes back as plain text ("Please limit requests..."), sometimes with 200.
            if resp.status_code == 429 or text.startswith("Please limit requests"):
                self.rate_limited += 1
                err = "rate limited (429)"
                time.sleep(15 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise GdeltError(f"HTTP {resp.status_code}: {text[:200]}")
            if not text:
                return []
            try:
                return json.loads(text).get("articles", [])
            except json.JSONDecodeError:
                # Query errors (e.g. "keyword too short") come back as plain text.
                raise GdeltError(text[:200])
        raise (RateLimited if err.startswith("rate limited") else GdeltError)(err)


def fetch_window(client, query, start, end):
    """All articles for [start, end), splitting any slice that hits the 250 cap.

    Returns (articles, saturated) where saturated counts MIN_SLICE slices that were
    still full, i.e. where some articles were necessarily missed.
    """
    articles = client.artlist(query, start, end)
    if len(articles) < MAX_RECORDS:
        return articles, 0
    if end - start <= MIN_SLICE:
        return articles, 1
    mid = start + (end - start) / 2
    a1, s1 = fetch_window(client, query, start, mid)
    a2, s2 = fetch_window(client, query, mid, end)
    return a1 + a2, s1 + s2


def normalize(article, query_id, fetched_at):
    """GDELT ArtList item -> the shared EuroLens news record (same shape as RSS records)."""
    url = article.get("url") or ""
    country = article.get("sourcecountry") or None
    language = article.get("language") or None
    return {
        "id": hashlib.sha1(normalize_url(url).encode()).hexdigest(),
        "url": url,
        "title": strip_html(article.get("title")),
        "snippet": None,                          # ArtList has no snippet
        "published_at": seendate_iso(article.get("seendate")),  # GDELT first-seen time, not the outlet's
        "fetched_at": fetched_at,
        "feed_id": f"gdelt:{query_id}",
        "source_name": article.get("domain") or None,
        "source_country_raw": COUNTRY_NAMES.get(country, country),  # ISO-2 if European, else GDELT's name
        "tier": "gdelt",
        "lang": LANGUAGES.get(language),
        "license": "aggregator",
        "source_type": "gdelt",
        "raw": {"sourcecountry": country, "language": language, "seendate": article.get("seendate")},
    }


def probe(hours):
    """Measure each query over the last `hours`, writing nothing but a report."""
    client = Client()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - LAG
    start = end - timedelta(hours=hours)
    rows, ids = [], set()
    t0 = time.monotonic()
    for qid, query in QUERIES.items():
        calls0, rl0, q0 = client.calls, client.rate_limited, time.monotonic()
        row = {"query": qid, "articles": None, "saturated": 0, "error": None}
        try:
            arts, row["saturated"] = fetch_window(client, query, start, end)
            row["articles"] = len(arts)
            new = {normalize(a, qid, iso(end))["id"] for a in arts}
            row["new_vs_earlier_queries"] = len(new - ids)
            ids |= new
        except (GdeltError, RateLimited) as e:
            row["error"] = str(e)
        row.update(calls=client.calls - calls0, rate_limited=client.rate_limited - rl0,
                   seconds=round(time.monotonic() - q0))
        rows.append(row)
        print(json.dumps(row), flush=True)

    per_day = 24 / hours
    a_total = sum(r["articles"] or 0 for r in rows if r["query"].startswith("A_"))
    lines = [
        f"## GDELT probe: {hours}h window {iso(start)} to {iso(end)}",
        f"- calls {client.calls}, rate-limited {client.rate_limited}, "
        f"elapsed {round(time.monotonic() - t0)}s",
        f"- unique articles {len(ids)} (≈ {round(len(ids) * per_day):,}/day)",
        f"- Axis A articles {a_total} (≈ {round(a_total * per_day):,}/day)",
        f"- queries with errors: {sum(1 for r in rows if r['error'])}, "
        f"saturated 15-min slices: {sum(r['saturated'] for r in rows)}",
        "", "| query | articles | ≈/day | calls | 429s | saturated | error |", "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        n = r["articles"]
        lines.append(f"| {r['query']} | {n if n is not None else '—'} | "
                     f"{round(n * per_day) if n is not None else '—'} | {r['calls']} | "
                     f"{r['rate_limited']} | {r['saturated']} | {r['error'] or ''} |")
    report = "\n".join(lines)
    print("\n" + report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--hours", type=float, default=2)
    a = ap.parse_args()
    if not a.probe:
        sys.exit("only --probe is implemented so far (ingest lands in Stage 3)")
    probe(a.hours)


if __name__ == "__main__":
    main()
