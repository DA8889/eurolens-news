"""Test a feed live and append it to sources.yaml.

    python add_source.py URL --tier eu [--country DE] [--name "..."] [--lang de] [--via-google]

URL may be a feed or a homepage; for a homepage the page's advertised RSS/Atom
link is used. If the site blocks bots, --via-google adds a Google News
`site:` search for that domain instead. Also run by .github/workflows/add-source.yml
from the "Add a source" issue form. Exit code 1 with a reason on failure.
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import requests
import yaml

from collect import ROOT, TIMEOUT, USER_AGENT, normalize_url

SOURCES = ROOT / "sources.yaml"
COMMON_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
SECTION = "  # ===================== ADDED VIA add_source.py ====================="
COUNTRIES = ("AT BE BG CY CZ DE DK EE EL ES FI FR GB HR HU IE IT LT LU LV MT NL PL PT "
             "RO SE SI SK EU —").split()
LICENSE_BY_TIER = {
    "eu": "eu_reuse", "government": "psi_attribution", "parliament": "psi_attribution",
    "central_bank": "psi_attribution", "stat_office": "psi_attribution",
    "broadcaster": "broadcaster_snippet", "agency": "agency_free_attribution",
    "wire": "agency_free_attribution", "aggregator": "aggregator",
}


class Fail(Exception):
    pass


class FeedLinks(HTMLParser):
    """Collect <link rel="alternate" type="application/rss+xml|atom+xml" href=...>."""
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if (tag == "link" and "alternate" in (a.get("rel") or "").lower()
                and re.search(r"(rss|atom)\+xml", a.get("type") or "") and a.get("href")):
            self.hrefs.append(a["href"])


def fetch(url):
    """Return (parsed feed or None, response). Raises Fail when the site refuses us."""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        raise Fail(f"could not fetch {url}: {type(e).__name__}") from e
    if resp.status_code in (401, 403, 429) or resp.headers.get("x-amzn-waf-action"):
        raise Fail(f"{url} blocks bots (HTTP {resp.status_code}). "
                   "Re-submit with the Google News option to read it via a site: search.")
    if resp.status_code >= 400:
        raise Fail(f"{url} returned HTTP {resp.status_code}")
    parsed = feedparser.parse(resp.content)
    return (parsed if parsed.entries else None), resp


def find_feed(url):
    """URL is a feed, a page advertising one, or a site with a feed at a common path.
    Return (feed_url, parsed)."""
    parsed, resp = fetch(url)
    if parsed:
        return resp.url, parsed
    finder = FeedLinks()
    finder.feed(resp.text)
    candidates = [urljoin(resp.url, h) for h in finder.hrefs]
    candidates += [urljoin(resp.url, p) for p in COMMON_PATHS]
    for candidate in candidates:
        try:
            parsed, resp2 = fetch(candidate)
        except Fail:
            continue
        if parsed:
            return resp2.url, parsed
    raise Fail(f"{url} is not a feed, advertises no working RSS/Atom feed, and has none at "
               f"{', '.join(COMMON_PATHS)}. Find the feed URL on the site and submit that instead.")


def google_news(url):
    host = urlsplit(url if "//" in url else "https://" + url).netloc.removeprefix("www.")
    feed_url = ("https://news.google.com/rss/search?q="
                + quote(f"site:{host} when:7d") + "&hl=en-GB&gl=GB&ceid=GB:en")
    parsed, _ = fetch(feed_url)
    if not parsed:
        raise Fail(f"Google News has no recent items for site:{host}")
    return feed_url, parsed, f"{host} (via Google News)"


def make_id(country, name, taken):
    prefix = {"EU": "eu", "—": "pan"}.get(country, country.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "feed"
    base = new = f"{prefix}_{slug}"
    n = 2
    while new in taken:
        new, n = f"{base}_{n}", n + 1
    return new


def add(url, tier, country="—", name=None, lang=None, via_google=False):
    if tier not in LICENSE_BY_TIER:
        raise Fail(f"tier must be one of {', '.join(LICENSE_BY_TIER)}")
    if country not in COUNTRIES:
        raise Fail(f"country must be one of {' '.join(COUNTRIES)}")
    text = SOURCES.read_text(encoding="utf-8")
    feeds = yaml.safe_load(text)["feeds"]
    existing = {normalize_url(f["url"]): f["id"] for f in feeds}
    if normalize_url(url) in existing:
        raise Fail(f"already in sources.yaml as {existing[normalize_url(url)]}")

    try:
        feed_url, parsed = find_feed(url)
        default_name = parsed.feed.get("title")
    except Fail:
        if not via_google:
            raise
        feed_url, parsed, default_name = google_news(url)
    if normalize_url(feed_url) in existing:
        raise Fail(f"its feed {feed_url} is already in sources.yaml as {existing[normalize_url(feed_url)]}")

    name = name or (default_name or urlsplit(feed_url).netloc).strip()
    feed_lang = (parsed.feed.get("language") or "").split("-")[0].lower() or None
    entry = {
        "id": make_id(country, name, {f["id"] for f in feeds}),
        "tier": tier, "country": country, "name": name, "url": feed_url,
        "lang": lang or feed_lang, "confidence": "verified",
        "license": LICENSE_BY_TIER[tier], "note": f"added via add_source {date.today()}",
    }
    # JSON scalars are valid YAML, so json.dumps gives safe quoting/escaping.
    line = "  - {" + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                               for k, v in entry.items()) + "}\n"
    new_text = text if text.endswith("\n") else text + "\n"
    if SECTION not in new_text:
        new_text += "\n" + SECTION + "\n"
    new_text += line
    if yaml.safe_load(new_text)["feeds"][-1] != entry:
        raise Fail("internal error: appended line did not round-trip through YAML")
    SOURCES.write_text(new_text, encoding="utf-8")
    return entry, len(parsed.entries)


def parse_issue(body):
    """Read the "Add a source" issue form (### Label / value blocks) into add() kwargs.

    >>> parse_issue("### Feed or website URL\\n\\nhttps://ex.com/rss\\n\\n### Country\\n\\n— (pan-European or none)"
    ...             "\\n\\n### Tier\\n\\neu\\n\\n### Name (optional)\\n\\n_No response_\\n\\n### If blocked"
    ...             "\\n\\n- [X] If the site blocks bots, add it via Google News instead")
    {'url': 'https://ex.com/rss', 'tier': 'eu', 'country': '—', 'name': None, 'via_google': True}
    """
    fields = {}
    for block in re.split(r"^### ", body.replace("\r\n", "\n"), flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        fields[label.strip()] = value.strip()
    name = fields.get("Name (optional)", "")
    return {
        "url": fields.get("Feed or website URL", ""),
        "tier": fields.get("Tier", ""),
        "country": fields.get("Country", "—").split(" ")[0],
        "name": None if name in ("", "_No response_") else name,
        "via_google": "[x]" in fields.get("If blocked", "").lower(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--from-issue", action="store_true",
                    help="read the issue form from the ISSUE_BODY env var (used by the workflow)")
    ap.add_argument("--tier")
    ap.add_argument("--country", default="—")
    ap.add_argument("--name")
    ap.add_argument("--lang")
    ap.add_argument("--via-google", action="store_true")
    a = ap.parse_args()
    if a.from_issue:
        kwargs = parse_issue(os.environ.get("ISSUE_BODY", ""))
    elif a.url and a.tier:
        kwargs = dict(url=a.url, tier=a.tier, country=a.country, name=a.name, lang=a.lang,
                      via_google=a.via_google)
    else:
        ap.error("give URL and --tier, or --from-issue")
    try:
        entry, n = add(**kwargs)
    except Fail as e:
        print(f"Not added: {e}")
        sys.exit(1)
    print(f"Added `{entry['id']}`: {entry['name']} ({n} items right now)\n{entry['url']}")


if __name__ == "__main__":
    main()
