"""Check candidate feeds live before they go into sources.yaml (pair news).

    python verify_sources.py              every candidate in sources_candidates.yaml
    python verify_sources.py --gdelt      also probe GDELT's raw 15-minute Events files

Each candidate's URLs are tried in order with add_source.find_feed (a feed, a page advertising
one, or a feed at a common path). Per candidate it records the outcome, the feed found, its item
count, newest item and items per week, and a class: verified (parses, newest item within
STALE_DAYS), stale (parses, older) or failed. A feed found for two candidates (a site-wide feed
behind several pages) or already in sources.yaml is flagged. Nothing is added: add what passes
with add_source.py.

The GDELT probe reads lastupdate.txt, downloads the newest Events export and counts the rows
whose two actors are different EuroLens countries, before and after the filters of
PAIR_NEWS_SOURCES.md 3.1, to estimate how many articles a day an Events ingest would send to
scoring. It never writes anything to the archive.

Writes exports/verify_sources_<time>.md (and .xlsx when openpyxl is installed); in GitHub Actions
(verify-sources.yml) the markdown also goes to the run's summary page.
"""
import argparse
import csv
import io
import os
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import yaml

from add_source import Fail, find_feed
from collect import ROOT, USER_AGENT, entry_date, iso, normalize_url
from country_codes import CAMEO

STALE_DAYS = 60
GDELT_LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
# Events export columns (GDELT 2.0 Event Codebook): 61 tab-separated fields per row.
COL = {"actor1": 7, "actor2": 17, "root": 28, "sources": 32, "articles": 33, "url": 60}
KEEP_ROOTS = {f"{n:02d}" for n in [*range(1, 9), *range(10, 18)]}


def check(cand, now):
    row = {"id": cand["id"], "name": cand["name"], "country": cand["country"], "tier": cand["tier"],
           "lang": cand.get("lang"), "class": "failed", "feed_url": None, "feed_title": None,
           "items": 0, "newest": None, "items_per_week": None, "error": None}
    errors = []
    for url in cand["urls"]:
        try:
            feed_url, parsed = find_feed(url)
        except Fail as e:
            errors.append(str(e)[:160])
            continue
        dates = sorted(d for d in (entry_date(e) for e in parsed.entries) if d)
        row.update(feed_url=feed_url, feed_title=(parsed.feed.get("title") or "").strip() or None,
                   items=len(parsed.entries), newest=dates[-1] if dates else None)
        if len(dates) >= 2:
            span = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).total_seconds()
            if span > 0:
                row["items_per_week"] = round((len(dates) - 1) / (span / 86400) * 7, 1)
        fresh = row["newest"] and row["newest"] >= iso(now - timedelta(days=STALE_DAYS))
        row["class"] = "verified" if fresh else "stale"
        return row
    row["error"] = " | ".join(errors)
    return row


def probe_gdelt():
    """Markdown lines describing the newest GDELT Events export, or the failure."""
    out = ["", "## GDELT 2.0 Events (raw 15-minute files)", ""]
    try:
        resp = requests.get(GDELT_LASTUPDATE, timeout=30, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        export_url = next(line.split()[2] for line in resp.text.splitlines()
                          if line.strip().endswith(".export.CSV.zip"))
        z = requests.get(export_url, timeout=120, headers={"User-Agent": USER_AGENT})
        z.raise_for_status()
    except Exception as e:
        return out + [f"Failed: {type(e).__name__}: {str(e)[:300]}"]
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter="\t") if len(r) > COL["url"]]
    seen = Counter()
    dyad = []
    for r in rows:
        a1, a2 = r[COL["actor1"]], r[COL["actor2"]]
        seen.update(c for c in (a1, a2) if c in ("ROM", "ROU"))
        c1, c2 = CAMEO.get(a1), CAMEO.get(a2)
        if c1 and c2 and c1 != c2:
            dyad.append(r)
    kept = [r for r in dyad if r[COL["root"]] in KEEP_ROOTS
            and (int(r[COL["sources"]] or 0) >= 2 or int(r[COL["articles"]] or 0) >= 3)]
    urls = {normalize_url(r[COL["url"]]) for r in kept if r[COL["url"]]}
    return out + [
        f"- `lastupdate.txt`: HTTP {resp.status_code}; export `{export_url.rsplit('/', 1)[-1]}`: "
        f"HTTP {z.status_code}, {len(z.content):,} bytes",
        f"- Rows: {len(rows):,}; both actors different EuroLens countries: {len(dyad):,}; "
        f"after root-code and source filters: {len(kept):,}; distinct article URLs: {len(urls):,}",
        f"- About {len(urls) * 96:,} article URLs a day at this rate (one file x 96), before dedupe "
        "against the archive and before the title join",
        f"- Romania as ROM: {seen['ROM']}, as ROU: {seen['ROU']}",
    ]


def markdown(rows, now, gdelt_lines):
    counts = Counter(r["class"] for r in rows)
    lines = [f"# Candidate feeds checked {iso(now)}", "",
             f"{counts['verified']} verified, {counts['stale']} stale, {counts['failed']} failed "
             f"(stale = newest item older than {STALE_DAYS} days).", "",
             "| class | id | items | per week | newest | feed | flags or error |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        note = r["error"] or "; ".join(r["flags"])
        lines.append(f"| {r['class']} | {r['id']} | {r['items']} | {r['items_per_week'] or ''} | "
                     f"{(r['newest'] or '')[:10]} | {r['feed_url'] or ''} | {note.replace('|', '/')} |")
    return "\n".join(lines + gdelt_lines) + "\n"


def write_xlsx(path, rows):
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    header = ["class", "id", "name", "country", "tier", "lang", "items", "items_per_week", "newest",
              "feed_title", "feed_url", "flags", "error"]
    ws.append(header)
    for r in rows:
        ws.append([r[k] if k != "flags" else "; ".join(r["flags"]) for k in header])
    ws.freeze_panes = "A2"
    wb.save(path)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gdelt", action="store_true", help="also probe GDELT's raw Events files")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    cands = yaml.safe_load((ROOT / "sources_candidates.yaml").read_text(encoding="utf-8"))["candidates"]
    existing = {normalize_url(f["url"]): f["id"]
                for f in yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["feeds"]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda c: check(c, now), cands))
    found = Counter(normalize_url(r["feed_url"]) for r in rows if r["feed_url"])
    for r in rows:
        r["flags"] = []
        if r["feed_url"]:
            key = normalize_url(r["feed_url"])
            if found[key] > 1:
                r["flags"].append("same feed found for another candidate")
            if key in existing:
                r["flags"].append(f"already in sources.yaml as {existing[key]}")

    md = markdown(rows, now, probe_gdelt() if a.gdelt else [])
    (ROOT / "exports").mkdir(exist_ok=True)
    stem = ROOT / "exports" / f"verify_sources_{now:%Y%m%dT%H%MZ}"
    stem.with_suffix(".md").write_text(md, encoding="utf-8")
    wrote_xlsx = write_xlsx(stem.with_suffix(".xlsx"), rows)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    print(f"Wrote {stem.with_suffix('.md')}" + (f" and {stem.with_suffix('.xlsx')}" if wrote_xlsx else ""))


if __name__ == "__main__":
    main()
