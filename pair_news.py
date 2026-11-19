"""Pair news files for the EuroLens Relations pages: the stories between two countries over 90 days.

Called from stories.write() each run, after the events and storylines are built. No AI writes
anything here except English translations of headlines (stories.translate, phrasing only).

1. Event map. pairs.py tags headlines; cluster.py groups the last 72 hours of headlines into
   events whose ids change once their first headline leaves the window. So each pair-tagged
   headline in the window is mapped to an event id and stored in
   pairtags/events/YYYY-MM.jsonl {"id", "event", "articles"} (committed). A headline whose event
   already has a stored id among its members inherits that id, so an event keeps one id for 90
   days. A tagged headline that never met an event stands alone.
2. Headlines published before the window are dropped: feeds re-publish old items (89 of 95
   Estonian government headlines fetched on 17-19 Nov 2026 were from before November).
   Which events count for a pair (qualifies): at least 2 of its headlines carry the pair, or 1
   when the event has at most 2 headlines in all, so a lone ministry release still shows while a
   single stray tag in a big summit story does not.
3. Rows (merge). Events are strict (cluster.THRESHOLD 0.85), so one story often makes many: the
   Iceye bid for Avio was ten one-headline events on Finland-Italy. On a pair's page every
   headline already shares the two countries, so a pair's events are merged when their English
   headlines are alike (GROUP_THRESHOLD) within GROUP_DAYS, as the News tab groups coverage (owner,
   2026-11-19). Every tagged headline is translated once for this; the vectors are kept in
   PAIR_VECTORS between runs.
4. Output (deployed, not committed):
     site/pairs/{PAIR}.json   {"pair", "updated", "window_days", "events": [newest first, at most
                              MAX_EVENTS: {"event_id" (the row's oldest event), "story_id",
                              "story_name", "headline" (the lead's, verbatim: see central),
                              "headline_en", "first_seen", "last_seen", "articles": [the lead,
                              then the others newest first: {"title", "title_en" (null if
                              English or untranslated), "url", "source_name",
                              "source_country_iso", "language", "published_at"}]}]}
     site/pairs/index.json    {"updated", "pairs": {PAIR: {"events_90d", "last_event"}}}
   story_id is set only for storylines whose site/story/<id>.json this run writes, so links never
   break. A pair with no events gets no file: a missing file means no recent pair news.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np

import cluster
from collect import SITE, iso
from pairs import PAIRTAGS, read_pairtags

WINDOW_DAYS = 90
MAX_EVENTS = 30
# Tested 2026-11-19 on the preview pair files (129 events): at 0.6 the Iceye bid's seven headlines
# in four languages made one row and Estonia-Latvia's nine rows became five; 0.65 split the bid again.
GROUP_THRESHOLD = 0.6
GROUP_DAYS = 7
EVENT_MAP = PAIRTAGS / "events"
ENGLISH = cluster.EVENTS / "en"                       # stories.py's translation cache
PAIR_VECTORS = cluster.ROOT / ".events_cache" / "pair_vectors.npz"   # kept by actions/cache
FOLDER = SITE / "pairs"


def months(now):
    return sorted({f"{now - timedelta(days=d):%Y-%m}" for d in range(WINDOW_DAYS + 1)})


def read_jsonl(folder, now):
    for m in months(now):
        path = folder / f"{m}.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                yield from map(json.loads, f)


def read_event_map(now):
    """{headline id: {"event", "articles"}} for the last WINDOW_DAYS days; the latest line wins."""
    return {l["id"]: {"event": l["event"], "articles": l["articles"]} for l in read_jsonl(EVENT_MAP, now)}


def read_english(now):
    """{headline id: English title} from the translation cache, for the whole window."""
    return {l["id"]: l["en"] for l in read_jsonl(ENGLISH, now) if l.get("en")}


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


def english_of(line, english):
    return line["title"] if line.get("lang") == "en" else english.get(line["id"])


def vectors(lines, english):
    """{headline id: unit vector of its English title (cluster.EMBED_MODEL)}. Vectors of English or
    translated titles are kept in PAIR_VECTORS; an untranslated title is embedded as it is, for this
    run only, so it gets its English vector once translated."""
    have = {}
    if PAIR_VECTORS.exists():
        z = np.load(PAIR_VECTORS)
        if str(z["model"]) == cluster.EMBED_MODEL:
            have = dict(zip(z["ids"].tolist(), z["vecs"]))
    text = {l["id"]: english_of(l, english) for l in lines}
    todo = [i for i, t in text.items() if t and i not in have]
    if todo:
        have.update(zip(todo, cluster.embed([" ".join(text[i].split()) for i in todo]).astype(np.float16)))
    keep = {i: have[i] for i, t in text.items() if t}
    if keep:
        PAIR_VECTORS.parent.mkdir(exist_ok=True)
        np.savez(PAIR_VECTORS, model=cluster.EMBED_MODEL, ids=np.array(list(keep)), vecs=np.array(list(keep.values())))
    out = {i: v.astype(np.float32) for i, v in keep.items()}
    raw = [l for l in lines if not text[l["id"]]]
    if raw:
        out.update(zip([l["id"] for l in raw], cluster.embed([" ".join(l["title"].split()) for l in raw])))
    return {i: v / np.linalg.norm(v) for i, v in out.items()}


def merge(events, vecs):
    """[(event id, lines)] of one pair, with events about the same story merged: oldest first, an
    event joins the group whose centre (the mean of its headlines' vectors) is most similar, if the
    cosine is at least GROUP_THRESHOLD and the group has a headline from the GROUP_DAYS before the
    event's first. A group keeps its oldest event's id.

    >>> v = {"a": np.array([1.0, 0.0]), "b": np.array([0.8, 0.6]), "c": np.array([0.0, 1.0]), "d": np.array([1.0, 0.0])}
    >>> l = lambda i, d: {"id": i, "fetched_at": f"2026-11-{d:02d}T00:00:00Z", "published_at": None}
    >>> [(e, [x["id"] for x in ls]) for e, ls in merge([("e2", [l("b", 2)]), ("e1", [l("a", 1)]), ("e3", [l("c", 3)]), ("e4", [l("d", 20)])], v)]
    [('e1', ['a', 'b']), ('e3', ['c']), ('e4', ['d'])]
    """
    unit = lambda x: x / np.linalg.norm(x)
    groups = []   # [sum of vectors or None, newest stamp, event id, lines]
    for event, lines in sorted(events, key=lambda e: (min(map(stamp, e[1])), e[0])):
        vs = [vecs[l["id"]] for l in lines if l["id"] in vecs]
        total = np.sum(vs, axis=0) if vs else None
        since = iso(datetime.strptime(min(map(stamp, lines)), "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=GROUP_DAYS))
        sims = [(float(unit(g[0]) @ unit(total)), k) for k, g in enumerate(groups)
                if total is not None and g[0] is not None and g[1] >= since]
        best = max(sims, default=None)
        if best and best[0] >= GROUP_THRESHOLD:
            g = groups[best[1]]
            g[0], g[1] = g[0] + total, max(g[1], max(map(stamp, lines)))
            g[3].extend(lines)
        else:
            groups.append([total, max(map(stamp, lines)), event, list(lines)])
    return [(g[2], g[3]) for g in groups]


def central(lines, vecs):
    """The headline nearest the lines' centre, preferring one in English within 0.10 of the nearest,
    as cluster.build_events picks an event's title; the first line if none has a vector.

    >>> v = {"a": np.array([1.0, 0.0]), "b": np.array([0.8, 0.6]), "c": np.array([0.6, 0.8])}
    >>> [central([{"id": i, "lang": l} for i, l in x], v)["id"] for x in ([("a", "de"), ("b", "de"), ("c", "de")], [("a", "de"), ("b", "de"), ("c", "en")], [("z", "de")])]
    ['b', 'c', 'z']
    """
    have = [l for l in lines if l["id"] in vecs]
    if not have:
        return lines[0]
    centre = np.mean([vecs[l["id"]] for l in have], axis=0)
    sims = {l["id"]: float(vecs[l["id"]] @ centre) for l in have}
    english = [l for l in have if l.get("lang") == "en" and sims[l["id"]] >= max(sims.values()) - 0.10]
    return max(english or have, key=lambda l: sims[l["id"]])


def row(event, lines, story_of, english, vecs):
    lines = sorted(lines, key=stamp, reverse=True)
    lead = central(lines, vecs)
    story = next((story_of[l["id"]] for l in lines if l["id"] in story_of), None)
    return {
        "event_id": event,
        "story_id": story["id"] if story else None,
        "story_name": story.get("name") if story else None,
        "headline": lead["title"],
        "headline_en": english_of(lead, english),
        "first_seen": stamp(lines[-1]),
        "last_seen": stamp(lines[0]),
        "articles": [{"title": l["title"], "title_en": None if l.get("lang") == "en" else english.get(l["id"]),
                      "url": l["url"], "source_name": l["source_name"], "source_country_iso": l["source_country_raw"],
                      "language": l.get("lang"), "published_at": stamp(l)}
                     for l in [lead] + [l for l in lines if l is not lead]],
    }


def build(tags, event_map, story_of, english, vecs):
    """{pair: [rows, newest first]} from {headline id: pairtags line}.

    >>> tags = {"a": {"id": "a", "pairs": ["DE-FR"], "title": "x", "url": "u", "source_name": "S",
    ...               "source_country_raw": "FR", "lang": "en", "published_at": "2026-11-18T10:00:00Z", "fetched_at": "2026-11-18T10:05:00Z"},
    ...         "b": {"id": "b", "pairs": ["DE-FR", "DE-PL"], "title": "y", "url": "v", "source_name": "T",
    ...               "source_country_raw": "DE", "lang": "de", "published_at": None, "fetched_at": "2026-11-18T12:00:00Z"}}
    >>> out = build(tags, {"a": {"event": "e1", "articles": 5}, "b": {"event": "e1", "articles": 5}}, {}, {"b": "y en"}, {})
    >>> [(p, len(es), es[0]["event_id"], es[0]["headline_en"], len(es[0]["articles"])) for p, es in sorted(out.items())]
    [('DE-FR', 1, 'e1', 'y en', 2)]
    """
    groups = defaultdict(list)
    for line in tags.values():
        m = event_map.get(line["id"], {"event": line["id"], "articles": 1})
        for p in line["pairs"]:
            groups[p, m["event"]].append((line, m["articles"]))
    events = defaultdict(list)
    for (pair, event), members in groups.items():
        if qualifies(len(members), max(n for _, n in members)):
            events[pair].append((event, [l for l, _ in members]))
    return {pair: sorted((row(e, lines, story_of, english, vecs) for e, lines in merge(es, vecs)),
                         key=lambda r: (r["last_seen"], r["event_id"]), reverse=True)
            for pair, es in events.items()}


def update(now, events, developing, english, meter, translate):
    """Map this run's events, then write site/pairs/. `developing` are the storylines with a
    site/story/ file this run; `translate` is stories.translate."""
    since = iso(now - timedelta(days=WINDOW_DAYS))
    tags = {i: l for i, l in read_pairtags(now, WINDOW_DAYS + 1).items() if stamp(l) >= since}
    stored = read_event_map(now)
    new = map_events(events, set(tags), stored)
    if new:
        EVENT_MAP.mkdir(parents=True, exist_ok=True)
        with (EVENT_MAP / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(l) + "\n" for l in new)
        stored.update((l["id"], {"event": l["event"], "articles": l["articles"]}) for l in new)
    story_of = {m: s for s in developing for e in s["events"] for m in e["member_ids"]}
    english = translate(now, {**read_english(now), **english},
                        [(l["id"], l["title"], l.get("lang")) for l in tags.values()], meter)
    out = build(tags, stored, story_of, english, vectors(list(tags.values()), english))

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
          f"{sum(len(es) for es in out.values())} rows in {len(out)} pair files")
