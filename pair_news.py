"""Pair news files for the EuroLens Relations pages: the events between two countries over 90 days.

Called from stories.write() each run, after the events and storylines are built. No AI writes
anything here except English translations of headlines (stories.translate, phrasing only).

1. Event map. pairs.py tags headlines; cluster.py groups the last 72 hours of headlines into
   events whose ids change once their first headline leaves the window. So each pair-tagged
   headline in the window is mapped to an event id and stored in
   pairtags/events/YYYY-MM.jsonl {"id", "event", "articles"} (committed). A headline whose event
   already has a stored id among its members inherits that id, so an event keeps one id for 90
   days. A tagged headline that never met an event stands alone.
2. Which events count for a pair (qualifies): at least 2 of its headlines carry the pair, or 1
   when the event has at most 2 headlines in all, so a lone ministry release still shows while a
   single stray tag in a big summit story does not.
3. Output (deployed, not committed):
     site/pairs/{PAIR}.json   {"pair", "updated", "window_days", "events": [newest first, at most
                              MAX_EVENTS: {"event_id", "story_id", "story_name", "headline" (the
                              newest report's, verbatim), "headline_en", "first_seen", "last_seen",
                              "articles": [newest first]}]}
     site/pairs/index.json    {"updated", "pairs": {PAIR: {"events_90d", "last_event"}}}
   story_id is set only for storylines whose site/story/<id>.json this run writes, so links never
   break. A pair with no events gets no file: a missing file means no recent pair news.
"""
import json
from collections import Counter, defaultdict
from datetime import timedelta

from collect import SITE, iso, read_archive
from pairs import PAIRTAGS, read_pairtags

WINDOW_DAYS = 90
MAX_EVENTS = 30
EVENT_MAP = PAIRTAGS / "events"
FOLDER = SITE / "pairs"


def read_event_map(now):
    """{headline id: {"event", "articles"}} for the last WINDOW_DAYS days; the latest line wins."""
    out = {}
    for m in sorted({f"{now - timedelta(days=d):%Y-%m}" for d in range(WINDOW_DAYS + 1)}):
        path = EVENT_MAP / f"{m}.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.update((l["id"], {"event": l["event"], "articles": l["articles"]}) for l in map(json.loads, f))
    return out


def map_events(events, tagged, stored):
    """New event-map lines for the tagged headlines among this run's events.

    >>> events = [{"id": "h2", "articles": 3, "members": [{"id": "h2"}, {"id": "h3"}, {"id": "h4"}]},
    ...           {"id": "h9", "articles": 1, "members": [{"id": "h9"}]}]
    >>> map_events(events, {"h3", "h4", "h9"}, {"h1": {"event": "h1", "articles": 2}, "h4": {"event": "h1", "articles": 2}})
    [{'id': 'h3', 'event': 'h1', 'articles': 3}, {'id': 'h4', 'event': 'h1', 'articles': 3}, {'id': 'h9', 'event': 'h9', 'articles': 1}]
    """
    lines = []
    for e in events:
        ids = [m["id"] for m in e["members"]]
        known = Counter(stored[i]["event"] for i in ids if i in stored).most_common()
        event = min(k for k, n in known if n == known[0][1]) if known else e["id"]
        for i in ids:
            want = {"event": event, "articles": e["articles"]}
            if i in tagged and stored.get(i) != want:
                lines.append({"id": i, **want})
    return lines


def qualifies(n_tagged, n_articles):
    """Does an event count for a pair, given its headlines carrying the pair and its size?

    >>> qualifies(1, 1), qualifies(1, 2), qualifies(1, 6), qualifies(2, 6)
    (True, True, False, True)
    """
    return n_tagged >= 2 or n_articles <= 2


def stamp(line):
    return line.get("published_at") or line["fetched_at"]


def build(tags, event_map, story_of, english):
    """{pair: [events, newest first]} from {headline id: pairtags line}.

    >>> tags = {"a": {"id": "a", "pairs": ["DE-FR"], "title": "x", "url": "u", "source_name": "S",
    ...               "source_country_raw": "FR", "lang": "en", "published_at": "2026-11-18T10:00:00Z", "fetched_at": "2026-11-18T10:05:00Z"},
    ...         "b": {"id": "b", "pairs": ["DE-FR", "DE-PL"], "title": "y", "url": "v", "source_name": "T",
    ...               "source_country_raw": "DE", "lang": "de", "published_at": None, "fetched_at": "2026-11-18T12:00:00Z"}}
    >>> out = build(tags, {"a": {"event": "e1", "articles": 5}, "b": {"event": "e1", "articles": 5}}, {}, {"b": "y en"})
    >>> [(p, len(es), es[0]["event_id"], len(es[0]["articles"])) for p, es in sorted(out.items())]
    [('DE-FR', 1, 'e1', 2)]
    """
    groups = defaultdict(list)
    for line in tags.values():
        m = event_map.get(line["id"], {"event": line["id"], "articles": 1})
        for p in line["pairs"]:
            groups[p, m["event"]].append((line, m["articles"]))
    out = defaultdict(list)
    for (pair, event), members in groups.items():
        if not qualifies(len(members), max(n for _, n in members)):
            continue
        lines = sorted((l for l, _ in members), key=stamp, reverse=True)
        lead = lines[0]   # the newest report, so a row's date is its place in the newest-first list
        story = next((story_of[l["id"]] for l in lines if l["id"] in story_of), None)
        out[pair].append({
            "_lead": (lead["id"], lead.get("lang")),   # for translation; not written
            "event_id": event,
            "story_id": story["id"] if story else None,
            "story_name": story.get("name") if story else None,
            "headline": lead["title"],
            "headline_en": lead["title"] if lead.get("lang") == "en" else english.get(lead["id"]),
            "first_seen": stamp(lines[-1]),
            "last_seen": stamp(lines[0]),
            "articles": [{"title": l["title"], "url": l["url"], "source_name": l["source_name"],
                          "source_country_iso": l["source_country_raw"], "language": l.get("lang"),
                          "published_at": stamp(l)} for l in lines],
        })
    for events in out.values():
        events.sort(key=lambda e: (e["last_seen"], e["event_id"]), reverse=True)
    return out


def update(now, events, developing, english, meter, translate):
    """Map this run's events, then write site/pairs/. `developing` are the storylines with a
    site/story/ file this run; `translate` is stories.translate."""
    tags = read_pairtags(now, WINDOW_DAYS + 1)
    stored = read_event_map(now)
    new = map_events(events, set(tags), stored)
    if new:
        EVENT_MAP.mkdir(parents=True, exist_ok=True)
        with (EVENT_MAP / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(l) + "\n" for l in new)
        stored.update((l["id"], {"event": l["event"], "articles": l["articles"]}) for l in new)
    story_of = {m: s for s in developing for e in s["events"] for m in e["member_ids"]}
    out = build(tags, stored, story_of, english)
    leads = [e for es in out.values() for e in es[:MAX_EVENTS] if not e["headline_en"]]
    if leads:
        english = translate(now, english, [(e["_lead"][0], e["headline"], e["_lead"][1]) for e in leads], meter)
        for e in leads:
            e["headline_en"] = english.get(e["_lead"][0])
    for es in out.values():
        for e in es:
            del e["_lead"]

    FOLDER.mkdir(parents=True, exist_ok=True)
    updated = iso(now)
    keep = set()
    for pair, es in out.items():
        path = FOLDER / f"{pair}.json"
        path.write_text(json.dumps({"pair": pair, "updated": updated, "window_days": WINDOW_DAYS,
                                    "events": es[:MAX_EVENTS]}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        keep.add(path.name)
    for path in FOLDER.glob("*.json"):
        if path.name not in keep and path.name != "index.json":
            path.unlink()
    (FOLDER / "index.json").write_text(json.dumps(
        {"updated": updated, "pairs": {p: {"events_90d": len(es), "last_event": es[0]["last_seen"][:10]}
                                       for p, es in sorted(out.items())}}, separators=(",", ":")), encoding="utf-8")
    print(f"pair news: {len(tags)} tagged headlines, {len(new)} new event-map lines, "
          f"{sum(len(es) for es in out.values())} events in {len(out)} pair files")
