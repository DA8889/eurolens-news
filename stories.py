"""Link events into storylines over 30 days: one level above events, so that eight cards about
US strikes on Iran become one storyline you can open.

    python stories.py              events (as cluster.py) and storylines; write the site files
    python stories.py --review     local: compare thresholds, write exports/stories_review_<time>.xlsx
    python stories.py --at 2026-10-31T00:30    local: run as of that UTC time (back-testing)

Each run first does cluster.py's work (the events of the last 72 h and the day's snapshot),
then reads the shown events of the last DAYS daily snapshots (events/) plus the current ones.
An event repeated across snapshots, under the same or a new id, is merged by shared
headlines, and its newest record speaks for it. An event's centre is the mean of its
headline vectors; storylines group event centres oldest first with cluster.group() at
THRESHOLD, as events group headlines. A storyline keeps the id and name of the previous
run's storyline it shares most events with; a new one takes its first event's id.

AI only phrases (Haiku): event titles and strand headlines are translated to English once
(events/en/YYYY-MM.jsonl), and each storyline of 2+ events is named from its events' English
titles (prompts/story_name_v1.txt), renamed only once it has doubled in size. Storylines
with enough headlines get a short summary from summarize.py. Costs go to scores/costs.jsonl,
under score.py's daily budget.

Outputs:
    site/events.json                 cluster.py's file; each event also gets "title_en" and,
                                     when its storyline has 2+ events, "storyline"
    site/stories.json                storylines of 2+ events seen in the last 7 days, each with
                                     its summary or null, and the strands you follow with their
                                     tagged headlines (deployed)
    site/story/<id>.json             each of those storylines' events with their headlines per
                                     day and their headlines (deployed)
    stories/YYYY/MM/YYYY-MM-DD.json  daily snapshot: storyline ids, names, event ids (committed)
    .events_cache/centres.npz, .events_cache/stories.json
                                     snapshot events' centres and the previous run's storylines
                                     (kept by actions/cache; rebuilt from the snapshots if lost)
In collect.yml this runs only when the repository variable SCORING is "on".
"""
import argparse
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np

import cluster
import summarize
from collect import ARCHIVE, ROOT, SCORES, SITE, iso, read_archive, read_tags
from score import (COSTS, DAILY_BUDGET, MODEL, Meter, chat, gloss, parse_json, spent_today,
                   strip_em_dashes)

DAYS = 30
THRESHOLD = 0.75       # cosine similarity of an event's centre to a storyline's; 0.65 made a "Russia" blob
DEVELOPING_DAYS = 7    # Stories tab: storylines with an event seen this recently
SHOW_MAX = 100
EVENTS_PER_STORY = 60
STRAND_MAX = 100       # headlines per strand on the Stories tab
NAME_PROMPT = "story_name_v1"
RUN_BUDGET = 0.05
CACHE = ROOT / ".events_cache"
CENTRES = CACHE / "centres.npz"
STATE = CACHE / "stories.json"
STORIES = ROOT / "stories"
ENGLISH = cluster.EVENTS / "en"


def unit(v):
    return v / np.linalg.norm(v)


def load_records(now, events):
    """Shown events, newest first: this run's, then the daily snapshots back to DAYS days."""
    records = [{**e, "member_ids": [m["id"] for m in e["members"]], "snap": None} for e in events if e["shown"]]
    for d in range(DAYS):
        day = now - timedelta(days=d)
        path = cluster.EVENTS / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.json"
        if path.exists():
            snap = json.loads(path.read_text(encoding="utf-8"))
            records += [{**e, "snap": f"{day:%Y-%m-%d}"} for e in snap["events"]]
    return records


def key(r):
    return r["id"] if r["snap"] is None else f"{r['snap']}/{r['id']}"


def merge(records):
    """Records that share a headline are one event seen on several days. Groups of record
    indexes, each newest first, in order of their newest record.

    >>> merge([{"member_ids": ["a", "b"]}, {"member_ids": ["c"]}, {"member_ids": ["b", "d"]}])
    [[0, 2], [1]]
    """
    parent = list(range(len(records)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner = {}
    for i, r in enumerate(records):
        for m in r["member_ids"]:
            if m in owner:
                a, b = find(i), find(owner[m])
                parent[max(a, b)] = min(a, b)
            else:
                owner[m] = i
    groups = {}
    for i in range(len(records)):
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def centres_for(records, groups, items, vecs):
    """Centre of each merged event, from its newest record. This run's events use this run's
    vectors; a snapshot record's centre is cached the first time all its headlines are in the
    window, and if it never was (or the cache was lost) it comes from the headlines still in
    the window, or else from embedding its title."""
    at = {r["id"]: i for i, r in enumerate(items)}
    cached = {}
    if CENTRES.exists():
        z = np.load(CENTRES)
        if str(z["model"]) == cluster.EMBED_MODEL:
            cached = dict(zip(z["keys"].tolist(), z["vecs"].astype(np.float32)))
    keep = {}
    for r in records:
        if r["snap"] is not None:
            k = key(r)
            if k in cached:
                keep[k] = cached[k]
            elif all(m in at for m in r["member_ids"]):
                keep[k] = unit(vecs[[at[m] for m in r["member_ids"]]].mean(axis=0))
    out, todo = [], []
    for n, g in enumerate(groups):
        r = records[g[0]]
        inside = [at[m] for m in r["member_ids"] if m in at]
        if r["snap"] is None or (key(r) not in keep and inside):
            c = unit(vecs[inside].mean(axis=0))
            if r["snap"] is not None:
                keep[key(r)] = c
        elif key(r) in keep:
            c = keep[key(r)]
        else:
            c = None
            todo.append(n)
        out.append(c)
    if todo:
        for n, c in zip(todo, cluster.embed([" ".join(records[groups[n][0]]["title"].split()) for n in todo])):
            out[n] = keep[key(records[groups[n][0]])] = c
    CACHE.mkdir(exist_ok=True)
    np.savez(CENTRES, model=cluster.EMBED_MODEL, keys=np.array(list(keep) or [""]),
             vecs=np.array(list(keep.values()) or [np.zeros(vecs.shape[1])], dtype=np.float16))
    # rounded like the cache, so a centre is the same whether computed now or read back
    return np.array(out, dtype=np.float16).astype(np.float32), len(todo)


def merged_events(records, groups):
    out = []
    for g in groups:
        rs = [records[i] for i in g]
        r = rs[0]
        days = {}
        for x in rs:
            for d, n in (x.get("days") or {}).items():
                days[d] = max(days.get(d, 0), n)
        out.append({"id": r["id"], "title": r["title"], "title_id": r.get("title_id"), "url": r.get("url"),
                    "lang": r.get("lang"), "first_seen": min(x["first_seen"] for x in rs),
                    "last_seen": max(x["last_seen"] for x in rs), "articles": r["articles"],
                    "outlets": r["outlets"], "countries": r["countries"], "relevance": r["relevance"],
                    "significance": r["significance"], "rank": r["rank"], "days": dict(sorted(days.items())),
                    "current": r["snap"] is None, "record_ids": sorted({x["id"] for x in rs}),
                    "member_ids": sorted({m for x in rs for m in x["member_ids"]})})
    return out


def age(now, stamp):
    """Days since an ISO time.

    >>> age(datetime(2026, 11, 5, 12, tzinfo=timezone.utc), "2026-11-04T00:00:00Z")
    1.5
    """
    return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 86400


def join_close(labels, centres, threshold):
    """Grouping oldest first can leave two storylines whose centres end up closer than the
    threshold; join such pairs, closest first, keeping the older storyline's label.

    >>> c = np.array([[1, 0], [0.8, 0.6], [0.6, 0.8], [0, 1]], dtype=np.float32)
    >>> join_close(np.array([0, 1, 2, 3]), c, 0.9).tolist()
    [0, 1, 1, 3]
    """
    keys = [int(l) for l in np.unique(labels)]        # ascending: older storylines first
    sums = np.array([centres[labels == k].sum(axis=0) for k in keys])
    cs = sums / np.linalg.norm(sums, axis=1, keepdims=True)
    sims = cs @ cs.T
    np.fill_diagonal(sims, -1)
    alive = np.ones(len(keys), dtype=bool)
    while True:
        i, j = sorted(np.unravel_index(int(sims.argmax()), sims.shape))
        if sims[i, j] < threshold:
            break
        sums[i] += sums[j]
        cs[i] = unit(sums[i])
        labels[labels == keys[j]] = keys[i]
        alive[j] = False
        sims[i] = sims[:, i] = np.where(alive, cs @ cs[i], -1)
        sims[i, i] = -1
        sims[j] = sims[:, j] = -1                     # j is gone
    return labels


def build_stories(now, events, centres, threshold):
    """Storylines (dicts with their events, best first), from merged events and their centres."""
    order = sorted(range(len(events)), key=lambda i: (events[i]["first_seen"], events[i]["id"]))
    labels = np.empty(len(events), dtype=int)
    labels[order] = cluster.group(centres[order], threshold)
    labels = join_close(labels, centres, threshold)
    recent = iso(now - timedelta(days=DEVELOPING_DAYS))
    stories = {}
    for i in order:
        stories.setdefault(int(labels[i]), []).append(i)
    out = []
    for idx in stories.values():
        es = [events[i] for i in idx]
        fresh = [e for e in es if e["last_seen"] >= recent]
        days = {}
        for e in es:
            for d, n in e["days"].items():
                days[d] = days.get(d, 0) + n
        out.append({
            "id": es[0]["id"], "first_seen": es[0]["first_seen"], "last_seen": max(e["last_seen"] for e in es),
            "n_events": len(es), "articles": sum(days.values()),
            "countries": sorted({c for e in es for c in e["countries"]}),
            # its best recent event, fading a point a day since last seen, plus a bonus for how
            # many recent events: 1 event +0, 2 +1, 4 or more +2
            "rank": round(max(e["rank"] - age(now, e["last_seen"]) for e in fresh)
                          + min(2.0, math.log2(len(fresh))), 2) if fresh else 0,
            "days": dict(sorted(days.items())),
            "centre": unit(centres[idx].mean(axis=0)),
            "events": sorted(es, key=lambda e: (e["last_seen"], e["rank"]), reverse=True),
            "event_ids": {x for e in es for x in e["record_ids"]},
        })
    out.sort(key=lambda s: (-s["rank"], -s["n_events"], s["id"]))
    return out


def carry_ids(stories, previous):
    """Each storyline takes the id of the previous storyline it shares most event ids with (each
    previous id once); a new one keeps its first event's id. It keeps that storyline's name too,
    unless another previous storyline joined it with at least half as many events.

    >>> s = [{"id": "a", "event_ids": {"a", "b"}}, {"id": "c", "event_ids": {"c", "d", "e"}}]
    >>> p = [{"id": "x", "name": "X", "named_size": 2, "event_ids": ["b"]},
    ...      {"id": "y", "name": "Y", "named_size": 2, "event_ids": ["c", "d"]},
    ...      {"id": "z", "name": "Z", "named_size": 2, "event_ids": ["e"]}]
    >>> [(x["id"], x.get("name")) for x in carry_ids(s, p)]
    [('x', 'X'), ('y', None)]
    """
    owner = {e: n for n, p in enumerate(previous) for e in p["event_ids"]}
    pairs = {}
    for i, s in enumerate(stories):
        for e in s["event_ids"]:
            if e in owner:
                pairs[(i, owner[e])] = pairs.get((i, owner[e]), 0) + 1
    taken, matched = set(), {}
    for (i, n), _ in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if i not in matched and n not in taken:
            matched[i] = n
            taken.add(n)
    used = {previous[n]["id"] for n in taken}
    for i, s in enumerate(stories):
        if i in matched:
            p = previous[matched[i]]
            main = pairs[(i, matched[i])]
            joined = sum(v for (j, n), v in pairs.items() if j == i and n != matched[i])
            s.update(id=p["id"], name=None if 2 * joined >= main else p.get("name"), named_size=p.get("named_size"))
        elif s["id"] in used:
            s["id"] += "-2"
    return stories


def load_previous(now):
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))["stories"]
    for d in range(3):
        day = now - timedelta(days=d)
        path = STORIES / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["stories"]
    return []


def read_english(now):
    out = {}
    for d in (now, now.replace(day=1) - timedelta(days=1)):
        path = ENGLISH / f"{d:%Y-%m}.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.update((l["id"], l["en"]) for l in map(json.loads, f) if l.get("en"))
    return out


def translate(now, english, heads, meter, save=True):
    """English for each (headline id, title, lang) in `heads` not yet translated; stored once."""
    todo = {i: t for i, t, lang in heads if i and lang != "en" and i not in english}
    if not todo or meter.exhausted():
        return english
    got = gloss([{"id": i, "title": t} for i, t in todo.items()], meter)
    english.update(got)
    if save and got:
        ENGLISH.mkdir(parents=True, exist_ok=True)
        with (ENGLISH / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as f:
            f.writelines(json.dumps({"id": i, "en": t}, ensure_ascii=False) + "\n" for i, t in got.items())
    return english


def english_title(e, english):
    return e["title"] if e.get("lang") == "en" else english.get(e.get("title_id"))


def name_stories(stories, english, meter):
    """Haiku names storylines of 2+ events that have no name yet, or have doubled since."""
    system = (ROOT / "prompts" / f"{NAME_PROMPT}.txt").read_text(encoding="utf-8")
    todo = [s for s in stories if s["n_events"] >= 2 and not (s.get("name") and s["n_events"] < 2 * (s.get("named_size") or 1))]
    for i in range(0, len(todo), 20):
        if meter.exhausted():
            break
        chunk = todo[i:i + 20]
        lines = []
        for n, s in enumerate(chunk, 1):
            top = sorted(s["events"], key=lambda e: -e["rank"])[:8]
            lines.append(f"{n} | " + " || ".join(" ".join((english_title(e, english) or e["title"]).split()) for e in top))
        content, _, usage = chat(MODEL, system, "\n".join(lines), 25 * len(chunk) + 50)
        meter.add(usage)
        try:
            data = parse_json(content)
        except (ValueError, json.JSONDecodeError):
            continue
        for n, s in enumerate(chunk, 1):
            name = clean_name(data.get(str(n)))
            if name:
                s["name"], s["named_size"] = name, s["n_events"]


def clean_name(name):
    """A usable storyline name, or None.

    >>> clean_name('"US strikes on Iran."')
    'US strikes on Iran'
    >>> clean_name("Talks — and more talks") is None, clean_name("x " * 12) is None
    (False, True)
    """
    if not isinstance(name, str):
        return None
    name = strip_em_dashes(name).strip().strip('"\'“”‘’').rstrip(".").strip()
    return name if 1 <= len(name.split()) <= 8 else None


def strands_view(now, stories, english, meter, save):
    """Followed strands: tagged headlines newest first, and the storylines they fall in."""
    import yaml
    strands = yaml.safe_load((ROOT / "strands.yaml").read_text(encoding="utf-8"))["strands"]
    tags = read_tags(now, DAYS + 1)
    if not tags:
        return []
    scores = {s["id"]: s for s in read_archive(now, DAYS + 1, SCORES) if s["id"] in tags}
    items = sorted((r for r in read_archive(now, DAYS + 1, ARCHIVE) if r["id"] in tags),
                   key=lambda r: r["fetched_at"], reverse=True)
    story_of = {m: s for s in stories if s["n_events"] >= 2 for e in s["events"] for m in e["member_ids"]}
    out = []
    for st in strands:
        mine = [r for r in items if st["id"] in tags[r["id"]]]
        shown = mine[:STRAND_MAX]
        english = translate(now, english, [(r["id"], r["title"], r["lang"]) for r in shown], meter, save)
        overlap = {}
        for r in mine:
            if r["id"] in story_of:
                overlap[story_of[r["id"]]["id"]] = overlap.get(story_of[r["id"]]["id"], 0) + 1
        out.append({"id": st["id"], "name": st["name"], "description": " ".join(st["description"].split()),
                    "count": len(mine),
                    "items": [{"id": r["id"], "title": r["title"],
                               "title_en": r["title"] if r["lang"] == "en" else english.get(r["id"]),
                               "url": r["url"], "source_name": r["source_name"], "country": r["source_country_raw"],
                               "lang": r["lang"], "fetched_at": r["fetched_at"],
                               "relevance": scores.get(r["id"], {}).get("relevance"),
                               "storyline": story_of[r["id"]]["id"] if r["id"] in story_of else None}
                              for r in shown],
                    "storylines": [sid for sid, n in sorted(overlap.items(), key=lambda kv: -kv[1]) if n >= 2][:5]})
    return out


def public(s, english, summary):
    return {"id": s["id"], "name": s.get("name"), "first_seen": s["first_seen"], "last_seen": s["last_seen"],
            "n_events": s["n_events"], "articles": s["articles"], "countries": s["countries"], "rank": s["rank"],
            "days": s["days"],
            "events": [{"id": e["id"], "title": e["title"], "title_en": english_title(e, english), "url": e["url"],
                        "lang": e["lang"], "first_seen": e["first_seen"], "last_seen": e["last_seen"],
                        "articles": e["articles"], "outlets": e["outlets"], "countries": e["countries"],
                        "significance": e["significance"], "rank": e["rank"], "current": e["current"]}
                       for e in s["events"][:EVENTS_PER_STORY]],
            "summary": summary}


def detail(s, records, english):
    """A storyline's events as in public(), each with its headlines per day and its headlines
    newest first, for site/story/<id>.json. English titles only where one is already stored.

    >>> r = {"id": "h1", "title": "Budget beschlossen", "url": "https://orf.at/1", "source_name": "ORF",
    ...      "source_country_raw": "AT", "lang": "de", "published_at": "2026-11-04T10:00:00Z"}
    >>> s = {"id": "s1", "events": [{"id": "e1", "days": {"2026-11-04": 1}, "member_ids": ["h1", "gone"]}]}
    >>> m = detail(s, {"h1": r}, {"h1": "Budget passed"})["events"][0]["members"]
    >>> len(m), m[0]["title_en"], m[0]["country"]
    (1, 'Budget passed', 'AT')
    """
    events = []
    for e in s["events"][:EVENTS_PER_STORY]:
        rs = sorted((records[m] for m in e["member_ids"] if m in records),
                    key=lambda r: r.get("published_at") or r["fetched_at"], reverse=True)
        events.append({"id": e["id"], "days": e["days"],
                       "members": [{"id": r["id"], "title": r["title"], "url": r["url"],
                                    "source_name": r["source_name"], "country": r["source_country_raw"],
                                    "lang": r.get("lang"), "published_at": r.get("published_at") or r["fetched_at"],
                                    **({"title_en": english[r["id"]]}
                                       if r.get("lang") != "en" and r["id"] in english else {})}
                                   for r in rs]})
    return {"id": s["id"], "events": events}


def run(now, threshold=THRESHOLD, save=True):
    """Events, then storylines; with `save`, also English titles and names (Haiku).
    Returns (storylines, merged events, this run's events, English titles, meter)."""
    t0 = time.monotonic()
    items = cluster.load_window(now)
    if not items:
        print("stories: no scored headlines in the window, nothing to group")
        return None
    vecs, new = cluster.embed_cached(items)
    events = cluster.build_events(items, vecs, cluster.group(vecs, cluster.THRESHOLD))
    if save:
        cluster.write(now, events, time.monotonic() - t0)
    records = load_records(now, events)
    groups = merge(records)
    merged = merged_events(records, groups)
    centres, embedded = centres_for(records, groups, items, vecs)
    stories = build_stories(now, merged, centres, threshold)
    stories = carry_ids(stories, load_previous(now) if save else [])
    budget = min(RUN_BUDGET, DAILY_BUDGET - spent_today(now.strftime("%Y-%m-%d")))
    meter = Meter(max(budget, 0) if os.environ.get("OPENROUTER_API_KEY") else 0)   # no key: no titles or names
    english = read_english(now)
    multi = [s for s in stories if s["n_events"] >= 2]
    if save:
        current = [e for e in events if e["shown"]][:cluster.SHOW_MAX]
        english = translate(now, english, [(e["title_id"], e["title"], e["lang"]) for e in current]
                            + [(e["title_id"], e["title"], e["lang"])
                               for s in multi[:SHOW_MAX] for e in s["events"][:EVENTS_PER_STORY]], meter)
        name_stories(multi, english, meter)
    print(f"stories: {len(records)} event records -> {len(merged)} events -> {len(stories)} storylines "
          f"({len(multi)} with 2+ events); {embedded} centres from titles; {time.monotonic() - t0:.0f}s")
    return stories, merged, events, english, meter


def write(now, stories, events, english, meter):
    multi = [s for s in stories if s["n_events"] >= 2]
    recent = iso(now - timedelta(days=DEVELOPING_DAYS))
    developing = [s for s in multi if s["last_seen"] >= recent][:SHOW_MAX]
    story_of = {x: s for s in multi for e in s["events"] for x in e["record_ids"]}
    path = SITE / "events.json"
    site_events = json.loads(path.read_text(encoding="utf-8"))
    for e in site_events["events"]:
        s = story_of.get(e["id"])
        e["title_en"] = english_title(e, english)
        if s:
            e["storyline"], e["storyline_name"] = s["id"], s.get("name")
    path.write_text(json.dumps(site_events, ensure_ascii=False), encoding="utf-8")
    strands = strands_view(now, stories, english, meter, True)
    summaries = summarize.update(now, developing)
    (SITE / "stories.json").write_text(json.dumps(
        {"updated": iso(now), "days": DAYS, "threshold": THRESHOLD, "developing_days": DEVELOPING_DAYS,
         "storylines": [public(s, english, summaries[s["id"]]) for s in developing], "strands": strands},
        ensure_ascii=False), encoding="utf-8")
    # one file per storyline, fetched only when someone opens it; drop storylines no longer shown
    records = {r["id"]: r for r in read_archive(now, DAYS + 1, ARCHIVE)}
    folder = SITE / "story"
    folder.mkdir(exist_ok=True)
    keep = set()
    for s in developing:
        path = folder / f"{s['id']}.json"
        path.write_text(json.dumps({**detail(s, records, english), "updated": iso(now)},
                                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        keep.add(path.name)
    for path in folder.glob("*.json"):
        if path.name not in keep:
            path.unlink()
    state = [{"id": s["id"], "name": s.get("name"), "named_size": s.get("named_size"),
              "first_seen": s["first_seen"], "last_seen": s["last_seen"], "n_events": s["n_events"],
              "event_ids": sorted(s["event_ids"])} for s in multi]
    CACHE.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"updated": iso(now), "stories": state}, ensure_ascii=False), encoding="utf-8")
    snap = STORIES / f"{now:%Y}" / f"{now:%m}" / f"{now:%Y-%m-%d}.json"
    if not snap.exists():
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps({"created": iso(now), "threshold": THRESHOLD, "stories": state},
                                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if meter.calls:
        COSTS.parent.mkdir(exist_ok=True)
        with COSTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": now.strftime("%Y-%m-%d"), "run_at": iso(now), "job": "stories",
                                "cost": round(meter.spent, 6), "calls": meter.calls}) + "\n")
    print(f"stories: {len(developing)} developing, {len(strands)} strands, ${meter.spent:.4f} for {meter.calls} calls; top: "
          + " | ".join(f"{s.get('name') or s['events'][0]['title'][:40]} ({s['n_events']} events)" for s in developing[:4]))


JUDGE_STORY = ('Each numbered headline is the lead of one news event. Event 1 belongs to a storyline. For '
               'every other event, decide whether it belongs to the same ongoing storyline (the same story '
               'or its direct developments, not just the same broad topic or country). Return only JSON: '
               '{"different": [numbers of the events that belong to a different storyline]}')
JUDGE_PAIRS = ('Each numbered line holds the lead headlines of two news storylines separated by " || ". '
               'For each line, decide whether both are the same ongoing storyline (the same story or its '
               'direct developments, not just the same broad topic). Return only JSON: {"1": true, "2": false, ...}')


def review(now, thresholds, budget, pick=None):
    """Compare storyline thresholds with Haiku as a checker (evaluation only); write the workbook."""
    from concurrent.futures import ThreadPoolExecutor
    from openpyxl import Workbook
    from gdelt_gal import add_sheet

    checker = Meter(budget)

    def ask(system, lines, max_tokens):
        content, _, usage = chat(MODEL, system, "\n".join(lines), max_tokens)
        checker.add(usage)
        try:
            return parse_json(content)
        except (ValueError, json.JSONDecodeError):
            return None

    rows, results = [], {}
    for t in thresholds:
        stories, merged, events, english, meter = run(now, t, save=False)
        multi = [s for s in stories if s["n_events"] >= 2]
        top = multi[:30]
        english = translate(now, english, [(e["title_id"], e["title"], e["lang"]) for s in top for e in s["events"][:15]],
                            checker, save=False)
        lead = lambda e: " ".join((english_title(e, english) or e["title"]).split())

        def purity(s):
            es = sorted(s["events"], key=lambda e: -e["rank"])[:15]
            got = ask(JUDGE_STORY, [f"{n} | {lead(e)}" for n, e in enumerate(es, 1)], 200)
            if not got or not isinstance(got.get("different"), list):
                return None
            return len(es) - 1, len({int(d) for d in got["different"] if str(d).isdigit() and 2 <= int(d) <= len(es)})

        with ThreadPoolExecutor(8) as pool:
            judged = [p for p in pool.map(purity, top) if p]
        cs = np.array([s["centre"] for s in stories])
        pairs = []
        for s in top:
            sims = cs @ s["centre"]
            sims[stories.index(s)] = -1
            b = int(sims.argmax())
            if t - 0.10 <= sims[b] < t:
                pairs.append(f"{lead(sorted(s['events'], key=lambda e: -e['rank'])[0])} || "
                             f"{lead(sorted(stories[b]['events'], key=lambda e: -e['rank'])[0])}")
        answers = ask(JUDGE_PAIRS, [f"{n} | {p}" for n, p in enumerate(pairs, 1)], 20 * len(pairs) + 50) if pairs else {}
        same = sum(1 for v in (answers or {}).values() if v is True)
        checked, wrong = sum(p[0] for p in judged), sum(p[1] for p in judged)
        results[t] = {"stories": stories, "english": english, "purity": 1 - wrong / checked if checked else 0,
                      "missed": same / len(pairs) if pairs and answers else 0}
        rows.append([t, len(merged), len(stories), len(multi), max(s["n_events"] for s in stories),
                     round(100 * results[t]["purity"], 1), checked, round(100 * results[t]["missed"], 1), len(pairs)])
        print(f"threshold {t}: {len(multi)} storylines of 2+ events, largest {rows[-1][4]}, purity {rows[-1][5]}%, "
              f"missed merges {rows[-1][7]}% of {len(pairs)} near pairs")

    pick = pick or max(thresholds, key=lambda t: results[t]["purity"] * (1 - results[t]["missed"]))
    stories, english = results[pick]["stories"], results[pick]["english"]
    top = [s for s in stories if s["n_events"] >= 2][:30]
    name_stories(top, english, checker)
    out = ROOT / "exports" / f"stories_review_{now:%Y%m%dT%H%MZ}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb = Workbook(write_only=True)
    add_sheet(wb, "Summary", ["threshold", "events", "storylines", "storylines_2plus", "largest", "purity_pct",
                              "events_checked", "missed_merge_pct", "near_pairs_checked"], rows)
    add_sheet(wb, "Storylines", ["rank", "storyline_name", "n_events", "event_title_en", "first_seen", "last_seen",
                                 "outlets", "countries", "headline", "url", "your_verdict", "your_note",
                                 "storyline_id", "event_id"],
              [[n, s.get("name") or "", s["n_events"], english_title(e, english) or "", e["first_seen"][:16],
                e["last_seen"][:16], e["outlets"], " ".join(e["countries"]), e["title"], e["url"] or "", "", "",
                s["id"], e["id"]]
               for n, s in enumerate(top, 1) for e in sorted(s["events"], key=lambda e: -e["rank"])[:15]])
    strands = strands_view(now, stories, english, checker, False)
    add_sheet(wb, "Strands", ["strand", "title_en", "headline", "source", "country", "relevance", "fetched_at",
                              "url", "your_verdict", "your_note", "headline_id"],
              [[st["name"], r["title_en"] or "", r["title"], r["source_name"], r["country"], r["relevance"],
                r["fetched_at"][:16], r["url"], "", "", r["id"]] for st in strands for r in st["items"][:25]])
    ws = wb.create_sheet("Notes")
    for line in [f"Storylines over {DAYS} days to {now:%Y-%m-%d %H:%M} UTC (only the days with event snapshots)",
                 f"Threshold for the Storylines sheet: {pick} (default: best purity x (1 - missed-merge rate); --pick overrides)",
                 "Purity: share of events Haiku judged to belong to the same storyline as its top event (top 30 storylines, up to 15 events each)",
                 "Missed merges: share of near pairs (each top storyline and its most similar other storyline, within 0.10 below the threshold) Haiku judged the same storyline",
                 "Storylines sheet: mark events that do not belong (your_verdict = X) and storylines that should be one (your_note)",
                 "Strands sheet: up to 25 newest tagged headlines per strand; mark headlines that do not belong (your_verdict = X)",
                 f"Checker, translation and naming cost: ${checker.spent:.4f} for {checker.calls} calls (Haiku)"]:
        ws.append([line])
    wb.save(out)
    print(f"storylines sheet at threshold {pick}; cost ${checker.spent:.4f}; wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--review", action="store_true", help="compare thresholds (local; uses Haiku as checker)")
    ap.add_argument("--thresholds", type=float, nargs="+", default=[0.70, 0.75, 0.80])
    ap.add_argument("--pick", type=float, help="threshold for the review's Storylines sheet")
    ap.add_argument("--budget", type=float, default=0.10, help="USD cap for the --review checker")
    ap.add_argument("--at", help="local testing: run as if it were this UTC time, e.g. 2026-10-31T00:30")
    a = ap.parse_args()
    if a.review and not os.environ.get("OPENROUTER_API_KEY"):
        ap.error("--review needs OPENROUTER_API_KEY")
    now = datetime.fromisoformat(a.at).replace(tzinfo=timezone.utc) if a.at else datetime.now(timezone.utc)
    if a.review:
        review(now, a.thresholds, a.budget, a.pick)
        return
    got = run(now)
    if got:
        stories, merged, events, english, meter = got
        write(now, stories, events, english, meter)


if __name__ == "__main__":
    main()
