"""Free filters for a GDELT export: remove non-news before any AI step.

    python gdelt_filter.py exports/gdelt_gal_<start>_24h.jsonl.gz   # free check of the rules

Rules come from gdelt_filters.yaml and run in this order, the first match giving the
recorded reason: no title, old (published more than max_age_hours before the window
start), blocked site, forum site, non-news URL section, non-news title. Duplicate
headlines are removed last, among the articles that passed, so a story whose first copy
was on a blocked site still survives through another outlet. The kept copy is the
earliest published and records how many copies there were (coverage is a signal of
significance for later steps).

gdelt_score.py calls filter_articles() and puts the kept and removed articles in the one
workbook for the day. Run on its own, this script only prints the count per rule, so a
change to gdelt_filters.yaml can be checked for free. It writes no files.
"""
import argparse
import gzip
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from score import title_key

ROOT = Path(__file__).parent
COLUMNS = ["published", "source_country", "outlet", "domain", "language", "title",
           "description", "url"]


def parse_time(value):
    """ISO time -> aware datetime, or None (unknown is never treated as old)."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def load_rules(path):
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        "max_age": timedelta(hours=cfg["max_age_hours"]),
        "domains": {d: kind for kind, ds in cfg["block_domains"].items() for d in ds},
        "prefixes": [(kind, p) for kind, ps in cfg["block_domain_prefixes"].items() for p in ps],
        "sections": [(kind, re.compile(rx)) for kind, rx in cfg["url_sections"].items()],
        "titles": [(kind, re.compile(rx)) for kind, rx in cfg["titles"].items()],
    }


def blocked_domain(domain, blocked):
    """(kind, matched domain) if the domain or any parent domain is blocked.

    >>> blocked_domain("www.sportbild.bild.de", {"sportbild.bild.de": "sports"})
    ('sports', 'sportbild.bild.de')
    >>> blocked_domain("news.as.com", {"as.com": "sports"}), blocked_domain("bas.com", {"as.com": "sports"})
    (('sports', 'as.com'), None)
    """
    parts = (domain or "").lower().removeprefix("www.").split(".")
    for i in range(len(parts) - 1):
        d = ".".join(parts[i:])
        if d in blocked:
            return blocked[d], d
    return None


def reason_for(r, rules, cutoff):
    """(reason, rule) for the first rule that removes this article, or None to keep it."""
    title = (r.get("title") or "").strip()
    if not title:
        return "no_title", ""
    published = parse_time(r.get("date"))
    if published and published < cutoff:
        return "old", f"published {published:%Y-%m-%d %H:%M}, before {cutoff:%Y-%m-%d %H:%M}"
    hit = blocked_domain(r.get("domain"), rules["domains"])
    if hit:
        return f"site_{hit[0]}", hit[1]
    domain = (r.get("domain") or "").lower().removeprefix("www.")
    for kind, prefix in rules["prefixes"]:
        if domain.startswith(prefix):
            return f"site_{kind}", prefix
    path = urlsplit(r.get("url") or "").path.lower()
    for kind, rx in rules["sections"]:
        if m := rx.search(path):
            return f"section_{kind}", m.group(0)
    low = title.casefold()
    for kind, rx in rules["titles"]:
        if m := rx.search(low):
            return f"title_{kind}", m.group(0)
    return None


def row(r):
    return [r.get("date"), r.get("source_country"), r.get("outletName"), r.get("domain"),
            r.get("lang"), r.get("title"), r.get("desc"), r.get("url")]


def filter_articles(records, rules):
    """(kept, removed, window_start, window_end, cutoff). removed holds (record, reason, rule);
    kept records gain "copies", the number of outlets that ran the same headline."""
    window_start = min(parse_time(r["seen_in_file"]) for r in records)
    window_end = max(parse_time(r["seen_in_file"]) for r in records)
    cutoff = window_start - rules["max_age"]

    removed, passed = [], []
    for r in records:
        why = reason_for(r, rules, cutoff)
        if why:
            removed.append((r, *why))
        else:
            passed.append(r)

    # Duplicates last: earliest published copy is kept (unknown dates sort last).
    far = datetime.max.replace(tzinfo=window_start.tzinfo)
    passed.sort(key=lambda r: (parse_time(r.get("date")) or far, r["seen_in_file"]))
    first, kept = {}, []
    for r in passed:
        k = title_key(r["title"])
        if k in first:
            first[k]["copies"] += 1
            removed.append((r, "duplicate", f"same headline as {first[k]['domain']}"))
        else:
            first[k] = {**r, "copies": 1}
            kept.append(first[k])
    return kept, removed, window_start, window_end, cutoff


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", type=Path, help="exports/gdelt_gal_*.jsonl.gz from gdelt_gal.py")
    ap.add_argument("--rules", type=Path, default=ROOT / "gdelt_filters.yaml")
    a = ap.parse_args()

    with gzip.open(a.export, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    kept, removed, window_start, window_end, _ = filter_articles(records, load_rules(a.rules))
    n = len(records)
    print(f"window {window_start:%Y-%m-%d %H:%M} to {window_end:%Y-%m-%d %H:%M} UTC; input {n}")
    for why, c in Counter(why for _, why, _ in removed).most_common():
        print(f"  removed {why:28} {c:6}  {100 * c / n:5.1f}%")
    print(f"  kept {len(kept)} ({100 * len(kept) / n:.1f}%); kept + removed = input: "
          f"{len(kept) + len(removed) == n}")


if __name__ == "__main__":
    main()
