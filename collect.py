"""Poll every feed in sources.yaml, archive new items, build the Pages site.

Run by .github/workflows/collect.yml every 30 minutes; also runnable locally:
    python collect.py

Outputs:
    archive/YYYY/MM/YYYY-MM-DD.jsonl  append-only, one record per new item (committed)
    site/latest.json                  items from the last 48h, newest first (deployed, not committed)
    site/health.json                  per-feed status for this run
    site/feed.xml, site/feeds/CC.xml  Atom feeds: all sources, and one per country

Rule carried from the EuroLens spec: store link + source + short snippet only,
never re-host full text. Missing values are null, never faked.
"""
import calendar
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
ARCHIVE = ROOT / "archive"
SITE = ROOT / "site"
SITE_URL = "https://da8889.github.io/eurolens-news/"
USER_AGENT = "EuroLensNewsBot/0.1 (+https://github.com/DA8889/eurolens-news)"
SNIPPET_MAX = 300
SEEN_DAYS = 7          # cross-run dedup window
LATEST_HOURS = 48      # window for the web page and Atom feeds
ATOM_ALL_MAX = 500     # cap for the all-sources feed
TIMEOUT = 20

ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM)


def normalize_url(url):
    """https, lowercase host, no tracking params, no fragment, no trailing slash.

    >>> normalize_url("http://WWW.Example.com/a/?utm_source=x&id=3&fbclid=y#top")
    'https://www.example.com/a?id=3'
    """
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() != "fbclid"]
    return urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/") or "/",
                       urlencode(query), ""))


def unwrap_google(url):
    """Google Alerts wraps links in a redirect; return the real article URL.

    >>> unwrap_google("https://www.google.com/url?rct=j&sa=t&url=https://ex.com/a%3Fb%3D1&ct=ga")
    'https://ex.com/a?b=1'
    >>> unwrap_google("https://ex.com/a")
    'https://ex.com/a'
    """
    p = urlsplit(url)
    if p.netloc.endswith("google.com") and p.path == "/url":
        real = dict(parse_qsl(p.query)).get("url")
        if real:
            return real
    return url


def strip_html(text):
    """Strip tags and entities, collapse whitespace.

    >>> strip_html("EU <b>tariffs</b> &amp; trade")
    'EU tariffs & trade'
    """
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def clean_snippet(text):
    """Strip tags and entities, collapse whitespace, cap length; '' becomes None.

    >>> clean_snippet("<p>Hello&nbsp;<b>world</b></p>")
    'Hello world'
    >>> clean_snippet("   ") is None
    True
    """
    text = strip_html(text)
    if not text:
        return None
    return text if len(text) <= SNIPPET_MAX else text[:SNIPPET_MAX - 1].rstrip() + "…"


def strip_publisher(title, publisher):
    """Google News appends ' - Publisher' to every title; drop it.

    >>> strip_publisher("Talks resume - Le Monde", "Le Monde")
    'Talks resume'
    >>> strip_publisher("Talks resume", "Le Monde")
    'Talks resume'
    """
    suffix = f" - {publisher}"
    return title[:-len(suffix)] if publisher and title.endswith(suffix) else title


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_date(entry):
    """Feed-declared publish (or update) time as UTC ISO, or None if the feed gives none."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return iso(datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc))


def fetch_feed(feed, fetched_at):
    """Return (records, health) for one feed. Never raises: a dead feed is reported, not fatal."""
    health = {"feed_id": feed["id"], "name": feed["name"], "url": feed["url"],
              "country": feed.get("country"), "tier": feed.get("tier"),
              "ok": False, "http_status": None, "items": 0, "new": 0, "error": None}
    try:
        resp = requests.get(feed["url"], timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        health["http_status"] = resp.status_code
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:  # network, TLS, HTTP errors
        health["error"] = f"{type(e).__name__}: {e}"[:300]
        return [], health

    if not parsed.entries:
        # Usually an HTML page rather than a feed; feedparser's bozo text says why.
        reason = parsed.get("bozo_exception")
        health["error"] = f"no feed entries ({reason})" if reason else "no feed entries"
        return [], health

    records = []
    for e in parsed.entries:
        link = unwrap_google(e.get("link") or "")
        title = strip_html(e.get("title"))
        if not link or not title:
            continue
        source_name = feed["name"]
        publisher = (e.get("source") or {}).get("title")
        if publisher and "news.google.com" in feed["url"]:
            title = strip_publisher(title, publisher)
            source_name = f"{publisher} (via Google News)"
        url = normalize_url(link)
        records.append({
            "id": hashlib.sha1(url.encode()).hexdigest(),
            "url": link,
            "title": title,
            "snippet": clean_snippet(e.get("summary")),
            "published_at": entry_date(e),
            "fetched_at": fetched_at,
            "feed_id": feed["id"],
            "source_name": source_name,
            "source_country_raw": feed.get("country"),
            "tier": feed.get("tier"),
            "lang": feed.get("lang"),
            "license": feed.get("license"),
        })
    health["ok"] = True
    health["items"] = len(records)
    return records, health


def archive_path(day):
    return ARCHIVE / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.jsonl"


def read_archive(now, days):
    out = []
    for d in range(days):
        path = archive_path(now - timedelta(days=d))
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.extend(json.loads(line) for line in f if line.strip())
    return out


def sort_time(r):
    return r["published_at"] or r["fetched_at"]


def write_atom(path, title, self_url, items, updated):
    feed = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(feed, f"{{{ATOM}}}id").text = self_url
    ET.SubElement(feed, f"{{{ATOM}}}title").text = title
    ET.SubElement(feed, f"{{{ATOM}}}updated").text = updated
    ET.SubElement(feed, f"{{{ATOM}}}link", rel="self", href=self_url)
    ET.SubElement(feed, f"{{{ATOM}}}link", rel="alternate", href=SITE_URL)
    for r in items:
        entry = ET.SubElement(feed, f"{{{ATOM}}}entry")
        ET.SubElement(entry, f"{{{ATOM}}}id").text = f"tag:da8889.github.io,2026:{r['id']}"
        ET.SubElement(entry, f"{{{ATOM}}}title").text = r["title"]
        ET.SubElement(entry, f"{{{ATOM}}}link", href=r["url"])
        ET.SubElement(entry, f"{{{ATOM}}}updated").text = sort_time(r)
        if r["published_at"]:
            ET.SubElement(entry, f"{{{ATOM}}}published").text = r["published_at"]
        author = ET.SubElement(entry, f"{{{ATOM}}}author")
        ET.SubElement(author, f"{{{ATOM}}}name").text = r["source_name"]
        if r["snippet"]:
            ET.SubElement(entry, f"{{{ATOM}}}summary").text = r["snippet"]
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(feed).write(path, encoding="utf-8", xml_declaration=True)


def main():
    now = datetime.now(timezone.utc)
    fetched_at = iso(now)
    config = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    # fetch: false marks entries that are not plain feeds (GDELT API, query templates)
    # or are known-blocked; each carries a note saying why.
    feeds = [f for f in config["feeds"] if f.get("fetch", True)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda f: fetch_feed(f, fetched_at), feeds))

    seen = {r["id"] for r in read_archive(now, SEEN_DAYS)}
    new = []
    for records, health in results:
        for r in records:
            if r["id"] not in seen:
                seen.add(r["id"])
                new.append(r)
                health["new"] += 1

    if new:
        path = archive_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cutoff = iso(now - timedelta(hours=LATEST_HOURS))
    latest = sorted((r for r in read_archive(now, 3) if sort_time(r) >= cutoff),
                    key=sort_time, reverse=True)
    healths = [h for _, h in results]

    SITE.mkdir(exist_ok=True)
    (SITE / "latest.json").write_text(json.dumps(
        {"updated": fetched_at, "items": latest}, ensure_ascii=False), encoding="utf-8")
    (SITE / "health.json").write_text(json.dumps(
        {"updated": fetched_at, "feeds": healths}, ensure_ascii=False, indent=1), encoding="utf-8")

    write_atom(SITE / "feed.xml", "EuroLens news: all sources", SITE_URL + "feed.xml",
               latest[:ATOM_ALL_MAX], fetched_at)
    by_country = {}
    for r in latest:
        cc = r["source_country_raw"]
        by_country.setdefault("EU" if cc in (None, "—") else cc, []).append(r)
    for cc, items in by_country.items():
        write_atom(SITE / "feeds" / f"{cc}.xml", f"EuroLens news: {cc}",
                   SITE_URL + f"feeds/{cc}.xml", items, fetched_at)

    ok = sum(h["ok"] for h in healths)
    print(f"{fetched_at}  feeds ok {ok}/{len(healths)}  new items {len(new)}  "
          f"latest {len(latest)}  country feeds {len(by_country)}")


if __name__ == "__main__":
    main()
