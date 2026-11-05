"""Group recent headlines into events: the same story across outlets, countries and languages.

    python cluster.py              write site/events.json (and the day's snapshot)
    python cluster.py --review     local: compare thresholds, write exports/events_review_<day>.xlsx
    python cluster.py --at 2026-10-31T00:30    local: rebuild as of that UTC time (back-testing)

Reads headlines first seen in the last WINDOW_HOURS (archive/) with their scores (scores/) and
keeps those with relevance >= MIN_RELEVANCE; unscored headlines are skipped, never treated as 0.
Embeds the headlines with a multilingual model (fastembed, EMBED_MODEL) and groups them oldest
first: a headline joins the event whose centre (the mean of its members) is most similar, if
the cosine similarity is at least THRESHOLD; otherwise it starts a new event. An event's id is
its first headline's id, so ids stay stable from run to run. No AI writes anything here: an
event's title is one of its own headlines.

Outputs:
    site/events.json                 shown events, best first (deployed, not committed)
    events/YYYY/MM/YYYY-MM-DD.json   compact snapshot of the shown events, written by the first
                                     run of each UTC day (committed; stories.py links these)
In collect.yml, stories.py runs this step and then its own, only when the repository variable
SCORING is "on".
"""
import argparse
import json
import math
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np

from collect import ROOT, SCORES, SITE, iso, read_archive

# Tested 4 Nov 2026 against minishlab/potion-multilingual-128M, which is 12x faster but grouped
# almost only within one language. This model embeds about 40 headlines a second on a laptop, so
# vectors are kept between runs (EMBEDDINGS) and each run embeds only its new headlines.
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_CACHE = ROOT / ".fastembed_cache"            # kept between GitHub runs by actions/cache
EMBEDDINGS = ROOT / ".events_cache" / "embeddings.npz"   # the window's vectors, likewise
WINDOW_HOURS = 72
MIN_RELEVANCE = 4     # wider than "Most relevant" (6) so coverage can lift underrated stories
THRESHOLD = 0.85      # cosine similarity to an event's centre; 0.80 merged whole topics, 0.90 split stories
SHOW_MAX = 200
EVENTS = ROOT / "events"
NOT_COUNTRIES = (None, "—", "EU")        # pan-European outlets count as outlets, not countries


def load_window(now):
    """Scored headlines first seen in the window, oldest first, scores attached."""
    cutoff, end = iso(now - timedelta(hours=WINDOW_HOURS)), iso(now)
    scores = {s["id"]: s for s in read_archive(now, 4, SCORES)}
    items = []
    for r in read_archive(now, 4):
        s = scores.get(r["id"])
        if cutoff <= r["fetched_at"] <= end and s and s["relevance"] >= MIN_RELEVANCE:
            items.append({**r, "relevance": s["relevance"], "significance": s["significance"]})
    return sorted(items, key=lambda r: (r["fetched_at"], r["id"]))


def embed(titles):
    from fastembed import TextEmbedding   # only this script needs it; collection stays lean
    model = TextEmbedding(EMBED_MODEL, cache_dir=str(MODEL_CACHE))
    vecs = np.array(list(model.embed(titles, batch_size=256)), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def embed_cached(items):
    """Vectors for `items`, reusing EMBEDDINGS; saves the window's vectors back.
    Stored as float16, and new vectors are rounded the same way, so a headline's vector is
    identical whether it was just embedded or read back. Returns (vectors, newly embedded count)."""
    have = {}
    if EMBEDDINGS.exists():
        z = np.load(EMBEDDINGS)
        if str(z["model"]) == EMBED_MODEL:
            have = dict(zip(z["ids"].tolist(), z["vecs"]))
    todo = [r for r in items if r["id"] not in have]
    if todo:
        have.update(zip([r["id"] for r in todo],
                        embed([" ".join(r["title"].split()) for r in todo]).astype(np.float16)))
    vecs = np.array([have[r["id"]] for r in items], dtype=np.float16)
    EMBEDDINGS.parent.mkdir(exist_ok=True)
    np.savez(EMBEDDINGS, model=EMBED_MODEL, ids=np.array([r["id"] for r in items]), vecs=vecs)
    return vecs.astype(np.float32), len(todo)


def group(vecs, threshold):
    """Event label per headline (input oldest first); same input, same labels.

    >>> v = np.array([[1, 0], [0.96, 0.28], [0, 1]], dtype=np.float32)
    >>> group(v, 0.9).tolist()
    [0, 0, 1]
    """
    sums = np.zeros_like(vecs)
    centres = np.zeros_like(vecs)
    labels = np.empty(len(vecs), dtype=int)
    k = 0
    for i, v in enumerate(vecs):
        if k:
            sims = centres[:k] @ v
            j = int(sims.argmax())
            if sims[j] >= threshold:
                labels[i] = j
                sums[j] += v
                centres[j] = sums[j] / np.linalg.norm(sums[j])
                continue
        labels[i] = k
        sums[k] = centres[k] = v
        k += 1
    return labels


def build_events(items, vecs, labels):
    """Event records, best first. `shown` marks those for the Events view."""
    members = {}
    for i, label in enumerate(labels):
        members.setdefault(label, []).append(i)
    events = []
    for idx in members.values():
        centre = vecs[idx].mean(axis=0)
        centre /= np.linalg.norm(centre)
        sims = vecs[idx] @ centre
        best = int(sims.argmax())
        english = [n for n, i in enumerate(idx) if items[i]["lang"] == "en" and sims[n] >= sims[best] - 0.10]
        head = items[idx[max(english, key=lambda n: sims[n]) if english else best]]
        group_items = [items[i] for i in idx]
        countries = sorted({r["source_country_raw"] for r in group_items} - set(NOT_COUNTRIES))
        outlets = {r["source_name"].lower() for r in group_items}
        relevance = max(r["relevance"] for r in group_items)
        significance = max(r["significance"] for r in group_items)
        events.append({
            "id": items[idx[0]]["id"], "title": head["title"], "title_id": head["id"], "url": head["url"],
            "lang": head["lang"],
            "first_seen": items[idx[0]]["fetched_at"], "last_seen": items[idx[-1]]["fetched_at"],
            "articles": len(idx), "outlets": len(outlets), "countries": countries,
            "relevance": relevance, "significance": significance,
            "days": dict(sorted(Counter(r["fetched_at"][:10] for r in group_items).items())),   # headlines per day
            # coverage bonus: 1 country +0, 2 +1, 4 +2, 8 or more +3
            "rank": round(significance + min(3.0, math.log2(len(countries))) if countries else significance, 2),
            "shown": len(outlets) >= 2 and (relevance >= 6 or (len(countries) >= 3 and relevance >= MIN_RELEVANCE)),
            "members": sorted(({k: r[k] for k in ("id", "title", "url", "source_name", "source_country_raw",
                                                   "lang", "relevance", "significance", "published_at", "fetched_at")}
                               for r in group_items), key=lambda m: (-m["significance"], m["fetched_at"])),
        })
    events.sort(key=lambda e: (-e["rank"], -e["articles"], e["id"]))
    return events


def write(now, events, seconds):
    shown = [e for e in events if e["shown"]]
    SITE.mkdir(exist_ok=True)
    (SITE / "events.json").write_text(json.dumps(
        {"updated": iso(now), "window_hours": WINDOW_HOURS, "threshold": THRESHOLD, "model": EMBED_MODEL,
         "events": [{k: v for k, v in e.items() if k != "shown"} for e in shown[:SHOW_MAX]]},
        ensure_ascii=False), encoding="utf-8")
    snap = EVENTS / f"{now:%Y}" / f"{now:%m}" / f"{now:%Y-%m-%d}.json"
    if not snap.exists():
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps(
            {"created": iso(now), "window_hours": WINDOW_HOURS, "threshold": THRESHOLD, "model": EMBED_MODEL,
             "events": [{**{k: e[k] for k in ("id", "title", "title_id", "url", "lang", "first_seen", "last_seen",
                                               "articles", "outlets", "countries", "relevance", "significance",
                                               "rank", "days")},
                         "member_ids": [m["id"] for m in e["members"]]} for e in shown]},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"events: {len(events)} groups, {len(shown)} shown, {seconds:.0f}s; top: "
          + " | ".join(f"{e['title'][:50]} ({e['outlets']} outlets, {len(e['countries'])} countries)" for e in shown[:3]))


JUDGE_SAME = ('Headline 1 reports a news event. For every other numbered headline, decide whether it '
              'reports the same event (the same happening, not just the same topic). Return only JSON: '
              '{"different": [numbers of the headlines that report a different event]}')
JUDGE_PAIRS = ('Each numbered line holds two headlines separated by " || ". For each line, decide whether '
               'both report the same news event (the same happening, not just the same topic). '
               'Return only JSON: {"1": true, "2": false, ...} with one entry per line.')


def review(now, items, vecs, thresholds, budget, pick=None):
    """Compare thresholds with Haiku as a checker (evaluation only) and write the review workbook."""
    from concurrent.futures import ThreadPoolExecutor
    from openpyxl import Workbook
    from gdelt_gal import add_sheet
    from score import MODEL, Meter, chat, gloss, parse_json

    meter = Meter(budget)

    def ask(system, lines, max_tokens):
        content, _, usage = chat(MODEL, system, "\n".join(lines), max_tokens)
        meter.add(usage)
        try:
            return parse_json(content)
        except (ValueError, json.JSONDecodeError):
            return None

    def purity(e):
        """(members checked, members judged to be a different event), against the event's title."""
        ms = ([m for m in e["members"] if m["title"] == e["title"]][:1]
              + [m for m in e["members"] if m["title"] != e["title"]])[:15]
        got = ask(JUDGE_SAME, [f"{n} | {m['title']}" for n, m in enumerate(ms, 1)], 200)
        if got is None or not isinstance(got.get("different"), list):
            return None
        different = {int(d) for d in got["different"] if str(d).isdigit()}
        return len(ms) - 1, len({d for d in different if 2 <= d <= len(ms)})

    rows, results = [], {}
    for t in thresholds:
        t0 = time.monotonic()
        labels = group(vecs, t)
        seconds = time.monotonic() - t0
        events = build_events(items, vecs, labels)
        multi = [e for e in events if e["articles"] > 1]
        top = [e for e in events if e["shown"]][:100]
        with ThreadPoolExecutor(8) as pool:
            judged = [p for p in pool.map(purity, top) if p]
        # missed merges: each top event's most similar other event, just below the threshold
        centres = np.zeros((labels.max() + 1, vecs.shape[1]), dtype=np.float32)
        np.add.at(centres, labels, vecs)
        centres /= np.linalg.norm(centres, axis=1, keepdims=True)
        label_of = {items[i]["id"]: int(l) for i, l in enumerate(labels)}
        title_of = {int(l): None for l in set(labels.tolist())}
        for e in events:
            title_of[label_of[e["id"]]] = e["title"]
        pairs = []
        for e in top:
            a = label_of[e["id"]]
            sims = centres @ centres[a]
            sims[a] = -1
            b = int(sims.argmax())
            if t - 0.10 <= sims[b] < t:
                pairs.append(f"{title_of[a]} || {title_of[b]}")
        chunks = [pairs[i:i + 20] for i in range(0, len(pairs), 20)]
        with ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(lambda c: ask(JUDGE_PAIRS, [f"{n} | {p}" for n, p in enumerate(c, 1)], 20 * len(c) + 50), chunks))
        same = sum(1 for a in answers if a for v in a.values() if v is True)
        asked = sum(len(c) for c, a in zip(chunks, answers) if a)
        checked, wrong = sum(p[0] for p in judged), sum(p[1] for p in judged)
        results[t] = {"events": events, "purity": 1 - wrong / checked if checked else 0,
                      "missed": same / asked if asked else 0}
        multilingual = sum(len({m["lang"] for m in e["members"]}) >= 3 for e in top[:50])
        rows.append([t, len(items), len(events), len(multi), sum(e["shown"] for e in events),
                     max(e["articles"] for e in events), round(100 * results[t]["purity"], 1), checked,
                     round(100 * results[t]["missed"], 1), asked, multilingual, round(seconds, 1)])
        print(f"threshold {t}: {len(events)} groups, {rows[-1][4]} shown, purity {rows[-1][6]}%, "
              f"missed merges {rows[-1][8]}% of {asked} near pairs, {multilingual} of the top 50 in 3+ languages")

    # Purity alone rewards splitting stories into small (often single-language) groups,
    # so weigh it against missed merges.
    pick = pick or max(thresholds, key=lambda t: results[t]["purity"] * (1 - results[t]["missed"]))
    top = [e for e in results[pick]["events"] if e["shown"]][:40]
    en = gloss([{"id": m["id"], "title": m["title"]} for e in top for m in e["members"] if m["lang"] != "en"])
    out = ROOT / "exports" / f"events_review_{now:%Y%m%dT%H%MZ}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb = Workbook(write_only=True)
    add_sheet(wb, "Summary", ["threshold", "headlines", "groups", "groups_2plus", "shown", "largest",
                              "purity_pct", "members_checked", "missed_merge_pct", "near_pairs_checked",
                              "top50_in_3plus_languages", "grouping_seconds"], rows)
    english = lambda m: m["title"] if m["lang"] == "en" else en.get(m["id"]) or ""
    event_en = {e["id"]: english(next(m for m in e["members"] if m["title"] == e["title"])) for e in top}
    add_sheet(wb, "Events", ["rank", "event_title_en", "outlets", "countries", "n_countries", "relevance",
                             "significance", "is_title", "headline", "title_en", "source", "country", "lang",
                             "member_relevance", "url", "your_verdict", "your_note", "event_id"],
              [[n, event_en[e["id"]], e["outlets"], " ".join(e["countries"]), len(e["countries"]), e["relevance"],
                e["significance"], "Y" if m["title"] == e["title"] else "", m["title"], english(m),
                m["source_name"], m["source_country_raw"], m["lang"], m["relevance"], m["url"], "", "", e["id"]]
               for n, e in enumerate(top, 1) for m in e["members"]])
    ws = wb.create_sheet("Notes")
    for line in [f"Window: {WINDOW_HOURS} h to {now:%Y-%m-%d %H:%M} UTC; headlines with relevance >= {MIN_RELEVANCE}",
                 f"Model: {EMBED_MODEL}",
                 f"Threshold for the Events sheet: {pick} (default: best purity x (1 - missed-merge rate); --pick overrides)",
                 "Purity: share of member headlines Haiku judged to report the same event as the first (top 100 shown events, up to 15 members each)",
                 "Missed merges: share of near pairs (each top event and its most similar other event, similarity within 0.10 below the threshold) that Haiku judged the same event",
                 "Events sheet: the top 40 shown events at the recommended threshold. Mark members that do not belong (your_verdict = X) and events that should have merged (your_note)",
                 f"Checker cost: ${meter.spent:.4f} for {meter.calls} calls (Haiku, evaluation only)"]:
        ws.append([line])
    wb.save(out)
    print(f"events sheet at threshold {pick}; checker cost ${meter.spent:.4f}; wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--review", action="store_true", help="compare thresholds (local; uses Haiku as checker)")
    ap.add_argument("--thresholds", type=float, nargs="+", default=[0.80, 0.85, 0.90])
    ap.add_argument("--pick", type=float, help="threshold for the review's Events sheet")
    ap.add_argument("--budget", type=float, default=0.10, help="USD cap for the --review checker")
    ap.add_argument("--at", help="local testing: run as if it were this UTC time, e.g. 2026-10-31T00:30")
    a = ap.parse_args()
    now = datetime.fromisoformat(a.at).replace(tzinfo=timezone.utc) if a.at else datetime.now(timezone.utc)
    items = load_window(now)
    if not items:
        print("events: no scored headlines in the window, nothing to group")
        return
    t0 = time.monotonic()
    vecs, new = embed_cached(items)
    print(f"{len(items)} headlines, {new} newly embedded in {time.monotonic() - t0:.0f}s")
    if a.review:
        review(now, items, vecs, a.thresholds, a.budget, a.pick)
        return
    labels = group(vecs, THRESHOLD)
    write(now, build_events(items, vecs, labels), time.monotonic() - t0)


if __name__ == "__main__":
    main()
