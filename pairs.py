"""Tag scored headlines with the country pairs they are about, using Haiku.

    python pairs.py                     tag the headlines scored since the last run
    python pairs.py --test calibration/pairs_gold.csv [--repeats 3]   local: check the prompt
    python pairs.py --review [--repeats 3]                            local: labelling workbook

A pair is two of the 28 EuroLens countries (country_codes.py), stored alphabetically (pair_key:
"DE-PL", "GB-IE"). Candidates are headlines with relevance >= MIN_RELEVANCE, the set cluster.py
groups into events. Two Haiku steps: the tagger (prompts/PROMPT_VERSION.txt) proposes the pairs
each headline is about, 40 headlines a call, quoting the words that name each side (a pair whose
quotes are not in the headline is dropped); then the checker (prompts/CHECK_VERSION.txt) sees each
proposal on its own line and gives a short reason before a true/false verdict. Alone, the tagger
kept taking the outlet's country or the meeting's venue for a side (2026-11-19). Most headlines
have no pair. Only headlines with a confirmed pair are written, to
pairtags/YYYY/MM/<item's archive day>.jsonl:
    {"id", "pairs", "why": {pair: checker's reason}, "version", "fetched_at", "tagged_at",
     "title", "url", "source_name", "source_country_raw", "lang", "published_at"}
The display fields are copied so pair_news.py can build 90 days of pair files from this small
store alone. pairtags/progress.json works as tags/progress.json does for tag.py: a change to
either prompt re-tags the last BACKFILL_DAYS days, older lines keep their tags (read_pairtags decides which
line counts), and failed or unpaid calls are retried next run, never taken as "no pair". Costs
go to scores/costs.jsonl under score.py's daily budget.
In collect.yml this runs only when the repository variable SCORING is "on".
"""
import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from collect import ARCHIVE, ROOT, SCORES, iso, read_archive
from country_codes import COUNTRIES, NAMES, pair_key, parse_pair
from score import (COSTS, DAILY_BUDGET, MODEL, WORKERS, InsufficientCredits, Meter, chat, gloss, parse_json,
                   spent_today, title_key)

# v1 and v2 (2026-11-19, scratch only) took the outlet's country, or the language, for a side, and a
# meeting's venue for a party; v3 has Haiku quote the headline words naming each side.
PROMPT_VERSION = "pairs_v3"
CHECK_VERSION = "pairs_check_v1"
MIN_RELEVANCE = 4      # as cluster.py: pair news is shown as events
CHUNK = 40
RUN_BUDGET = 0.05      # USD per run; about $0.03-0.05 a day
BACKFILL_DAYS = 3      # after a prompt change
PAIRTAGS = ROOT / "pairtags"
PROGRESS = PAIRTAGS / "progress.json"
DISPLAY = ("title", "url", "source_name", "source_country_raw", "lang", "published_at")
OFFICIAL = ("government", "parliament", "central_bank")   # publishers that are a side of their own news


def system_prompt():
    text = (ROOT / "prompts" / f"{PROMPT_VERSION}.txt").read_text(encoding="utf-8")
    return text.replace("{countries}", "\n".join(f"  {c} {n}" for c, n in COUNTRIES))


def version(system):
    return hashlib.sha1(system.encode("utf-8")).hexdigest()[:12]


def prompts():
    """(tagger system prompt, checker system prompt, version: a hash of both)."""
    tagger = system_prompt()
    checker = (ROOT / "prompts" / f"{CHECK_VERSION}.txt").read_text(encoding="utf-8")
    return tagger, checker, version(tagger + checker)


def pair_line(n, r):
    """One numbered input line: N | language | outlet | headline. The outlet's country is given only
    for official publishers; for news outlets it misled v1 into tagging outlet country + subject.

    >>> pair_line(2, {"lang": "en", "source_name": "Estonian govt", "tier": "government", "source_country_raw": "EE", "title": "PM  visits Riga"})
    '2 | en | Estonian govt (official, EE) | PM visits Riga'
    >>> pair_line(3, {"lang": "nl", "source_name": "NOS", "tier": "broadcaster", "source_country_raw": "NL", "title": "Brexit"})
    '3 | nl | NOS | Brexit'
    """
    title = " ".join(r["title"].split())
    who = r.get("source_name") or "?"
    if r.get("tier") in OFFICIAL and r.get("source_country_raw") in NAMES:
        who = f"{who} (official, {r['source_country_raw']})"
    return f"{n} | {r.get('lang') or '?'} | {who} | {title}"


def final_json(content):
    """Parsed JSON of a reply; when Haiku corrects itself in prose after a first object ("Wait,
    that's incorrect... the correct answer is: {}"), the last object is its answer.

    >>> final_json('{"2": [["NL-GB", "Netherlands", "Middle East"]]}\\n\\nWait, that is wrong. The correct answer is:\\n\\n{}')
    {}
    >>> final_json('```json\\n{"1": []}\\n```')
    {'1': []}
    """
    try:
        return parse_json(content)
    except (ValueError, json.JSONDecodeError):
        decoder = json.JSONDecoder()
        for m in reversed(list(re.finditer(r"(?m)^\{", content or ""))):
            try:
                return decoder.raw_decode(content, m.start())[0]
            except json.JSONDecodeError:
                continue
        raise


def plain(text):
    return " ".join(str(text).replace("\u2019", "'").split()).casefold()


def grounded(item, r):
    """The pair key of one answer item [pair, words for one side, words for the other], or None
    unless both quotes are in the headline. "official" stands for an official publisher's own side.

    >>> r = {"title": "Minister met her Latvian counterpart", "tier": "government", "source_country_raw": "EE"}
    >>> grounded(["LV-EE", "official", "latvian"], r), grounded(["EE-PL", "official", "Polish"], r), grounded(["FI-LV", "official", "Latvian"], r)
    ('EE-LV', None, None)
    """
    if not (isinstance(item, list) and len(item) == 3 and all(isinstance(x, str) and x.strip() for x in item)):
        return None
    pair = parse_pair(item[0])
    if not pair:
        return None
    title = plain(r["title"])
    official = r.get("tier") in OFFICIAL and r.get("source_country_raw") in pair.split("-")
    ok = lambda q: plain(q) in title or (official and plain(q) == "official")
    return pair if ok(item[1]) and ok(item[2]) else None


def parse_pairs(content, records):
    """{line number: [pair keys]} for lines with at least one grounded pair (see grounded). Codes are
    made canonical (UK -> GB, GR -> EL) and sorted; codes outside the 28 and same-country pairs are
    dropped, and so is every pair of a line whose pairs span four or more countries (multilateral).

    >>> recs = [{"title": "Merz meets Tusk in Warsaw"}, {"title": "Kallas skipped as Poland joins talks"},
    ...         {"title": "Vienna, Berlin, Athens and Copenhagen back return hubs"}, {"title": "Cyprus hosts Greek PM, then flies to London"}]
    >>> parse_pairs('{"1": [["PL-DE", "Tusk", "merz"], ["DE-FR", "Merz", "Macron"]], "2": [["EE-PL", "Kallas", "Estonia"]], '
    ...             '"3": [["AT-DE", "Vienna", "Berlin"], ["EL-DK", "Athens", "Copenhagen"]], '
    ...             '"4": [["CY-EL", "Cyprus", "Greek PM"], ["UK-CY", "London", "Cyprus"]], "9": [["AT-DE", "a", "b"]]}', recs)
    {1: ['DE-PL'], 4: ['CY-EL', 'CY-GB']}
    """
    data = final_json(content)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    out = {}
    for k, v in data.items():
        if k.isdigit() and 1 <= int(k) <= len(records) and isinstance(v, list):
            got = list(dict.fromkeys(p for p in (grounded(x, records[int(k) - 1]) for x in v) if p))
            if got and len({c for p in got for c in p.split("-")}) < 4:
                out[int(k)] = got
    return out


def tag_chunk(records, system, meter):
    """{id: [pairs]} for every record ([] = none), or None if the model failed twice."""
    user = "\n".join(pair_line(i, r) for i, r in enumerate(records, 1))
    for _ in range(2):
        if meter.exhausted():
            return None
        content, finish, usage = chat(MODEL, system, user, 20 * len(records) + 100)
        meter.add(usage)
        if finish == "length":
            continue
        try:
            got = parse_pairs(content, records)
        except (ValueError, json.JSONDecodeError):
            continue
        return {r["id"]: got.get(i, []) for i, r in enumerate(records, 1)}
    return None


def tag_all(records, system, meter):
    """({id: [pairs]}, all chunks answered?) with exact-duplicate titles asked once."""
    groups = defaultdict(list)
    for r in records:
        groups[title_key(r["title"])].append(r)
    reps = [g[0] for g in groups.values()]
    chunks = [reps[i:i + CHUNK] for i in range(0, len(reps), CHUNK)]
    out, complete = {}, True
    with ThreadPoolExecutor(WORKERS) as pool:
        for got in pool.map(lambda c: tag_chunk(c, system, meter), chunks):
            if got is None:
                complete = False
            else:
                out.update(got)
    for g in groups.values():
        if g[0]["id"] in out:
            out.update({r["id"]: out[g[0]["id"]] for r in g[1:]})
    return out, complete


def parse_checks(content, n):
    """{line number: (reason, verdict)} for lines answered as [reason, true or false].

    >>> parse_checks('{"1": ["Slovak jets defend Polish airspace", true], "2": ["Spanish paper", false], "3": "yes", "9": ["x", true]}', 3)
    {1: ('Slovak jets defend Polish airspace', True), 2: ('Spanish paper', False)}
    """
    data = final_json(content)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return {int(k): (str(v[0]), v[1]) for k, v in data.items()
            if k.isdigit() and 1 <= int(k) <= n and isinstance(v, list) and len(v) == 2 and isinstance(v[1], bool)}


def check_chunk(items, system, meter):
    """{(id, pair): (reason, verdict)} for every (record, pair) item, or None if the model failed
    twice or left lines unanswered."""
    user = "\n".join(f"{pair_line(i, r)} || {' and '.join(NAMES[c] for c in p.split('-'))}"
                     for i, (r, p) in enumerate(items, 1))
    for _ in range(2):
        if meter.exhausted():
            return None
        content, finish, usage = chat(MODEL, system, user, 40 * len(items) + 60)
        meter.add(usage)
        if finish == "length":
            continue
        try:
            got = parse_checks(content, len(items))
        except (ValueError, json.JSONDecodeError):
            continue
        if len(got) == len(items):
            return {(r["id"], p): got[i] for i, (r, p) in enumerate(items, 1)}
    return None


def tag_and_check(records, tagger, checker, meter):
    """({id: [confirmed pairs]}, {id: [proposed pairs]}, {(id, pair): (reason, verdict)}, all answered?)
    The tagger proposes pairs for every record; the checker judges each proposal, once per title."""
    proposed, complete = tag_all(records, tagger, meter)
    by_id = {r["id"]: r for r in records}
    groups = defaultdict(list)
    for i, ps in proposed.items():
        for p in ps:
            groups[title_key(by_id[i]["title"]), p].append(i)
    items = [(by_id[ids[0]], p) for (_, p), ids in groups.items()]
    verdicts = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for got in pool.map(lambda c: check_chunk(c, checker, meter),
                            [items[k:k + CHUNK] for k in range(0, len(items), CHUNK)]):
            if got is None:
                complete = False
            else:
                verdicts.update(got)
    for (_, p), ids in groups.items():
        if (ids[0], p) in verdicts:
            verdicts.update({(i, p): verdicts[ids[0], p] for i in ids[1:]})
    confirmed = {i: [p for p in ps if verdicts.get((i, p), ("", False))[1]] for i, ps in proposed.items()}
    return confirmed, proposed, verdicts, complete


def read_pairtags(now, days):
    """{headline id: pairtags line} for the last `days` archive days. A line counts if it was made
    with the current prompt version, or if its headline is older than that version's back-fill;
    the latest line for an id wins (as collect.read_tags)."""
    if not PROGRESS.exists():
        return {}
    p = json.loads(PROGRESS.read_text(encoding="utf-8"))
    out = {}
    for line in read_archive(now, days, PAIRTAGS):
        if line["version"] == p["version"] or line["fetched_at"] < p["since"]:
            out[line["id"]] = line
    return {k: v for k, v in out.items() if v["pairs"]}


def candidates(now, days, since="", through=""):
    """Archive records with relevance >= MIN_RELEVANCE, scored after `through`, fetched since `since`,
    each with its relevance; and the latest scored_at among them."""
    scores = {s["id"]: s for s in read_archive(now, days, SCORES)
              if s["relevance"] >= MIN_RELEVANCE and s["scored_at"] > through}
    records = [{**r, "relevance": scores[r["id"]]["relevance"]} for r in read_archive(now, days, ARCHIVE)
               if r["id"] in scores and r["fetched_at"] >= since]
    return records, max((scores[r["id"]]["scored_at"] for r in records), default=None)


# ----------------------------------------------------------------------------------- local checks

def load_gold(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def score_run(gold, run):
    """(true positives, false positives, false negatives) of one run against the gold rows.
    A Y row is a pair the headline is about; any other pair the run gives that headline (including
    on a "none" row) is a false positive.

    >>> gold = [{"id": "a", "pair": "DE-FR", "expect": "Y"}, {"id": "a", "pair": "DE-PL", "expect": "N"},
    ...         {"id": "b", "pair": "none", "expect": "N"}, {"id": "c", "pair": "GB-IE", "expect": "Y"}]
    >>> score_run(gold, {"a": ["DE-FR", "DE-PL"], "b": ["AT-DE"], "c": []})
    (1, 2, 1)
    """
    truth = defaultdict(set)
    for g in gold:
        truth[g["id"]]
        if g["expect"] == "Y":
            truth[g["id"]].add(parse_pair(g["pair"]))
    tp = fp = fn = 0
    for i, want in truth.items():
        got = set(run.get(i, []))
        tp, fp, fn = tp + len(got & want), fp + len(got - want), fn + len(want - got)
    return tp, fp, fn


def test(path, repeats):
    """Tag the gold headlines `repeats` times; print precision and recall per run and the flips."""
    gold = load_gold(path)
    records = list({g["id"]: {"id": g["id"], "title": g["title"], "lang": g["lang"] or None,
                              "source_country_raw": g["country"] or None, "source_name": g["source"] or None,
                              "tier": g.get("tier") or None}
                    for g in gold}.values())
    tagger, checker, v = prompts()
    meter = Meter(budget=0.20)
    both = [tag_and_check(records, tagger, checker, meter) for _ in range(repeats)]
    runs = [b[0] for b in both]
    precision, recall = [], []
    for n, (run, proposed, *_) in enumerate(both, 1):
        tp, fp, fn = score_run(gold, run)
        precision.append(tp / (tp + fp) if tp + fp else 1.0)
        recall.append(tp / (tp + fn) if tp + fn else 1.0)
        ptp, pfp, pfn = score_run(gold, proposed)
        print(f"run {n}: precision {precision[-1]:.3f} ({tp} of {tp + fp}), recall {recall[-1]:.3f} ({tp} of {tp + fn});"
              f" tagger alone: precision {ptp / (ptp + pfp) if ptp + pfp else 1:.3f}, recall {ptp / (ptp + pfn) if ptp + pfn else 1:.3f}")
    flips = sum(1 for r in records if len({tuple(sorted(run.get(r["id"], []))) for run in runs}) > 1)
    for g in gold:
        hits = sum(1 for run in runs if (run.get(g["id"]) if g["pair"] == "none" else parse_pair(g["pair"]) in run.get(g["id"], [])))
        ok = hits * 2 > repeats if g["expect"] == "Y" else hits == 0
        if not ok:
            print(f"FAIL  {g['expect']} {g['pair']:<6} {hits}/{repeats}  {g.get('title_en') or g['title']}"
                  f"  [{'; '.join(' '.join(run.get(g['id'], [])) or '-' for run in runs)}]")
    print(f"median precision {statistics.median(precision):.3f}, median recall {statistics.median(recall):.3f}; "
          f"{flips} of {len(records)} headlines changed between runs; ${meter.spent:.4f} for {meter.calls} calls; "
          f"version {v}")


# English names and adjectives, only to find likely misses for the review workbook (untagged
# English headlines naming two countries); tagging itself never uses them.
WORDS = {"AT": "Austria|Austrian", "BE": "Belgium|Belgian", "BG": "Bulgaria|Bulgarian", "CY": "Cyprus|Cypriot",
         "CZ": "Czechia|Czech", "DE": "Germany|German", "DK": "Denmark|Danish", "EE": "Estonia|Estonian",
         "EL": "Greece|Greek", "ES": "Spain|Spanish", "FI": "Finland|Finnish", "FR": "France|French",
         "GB": "Britain|British|UK|United Kingdom", "HR": "Croatia|Croatian", "HU": "Hungary|Hungarian",
         "IE": "Ireland|Irish", "IT": "Italy|Italian", "LT": "Lithuania|Lithuanian", "LU": "Luxembourg",
         "LV": "Latvia|Latvian", "MT": "Malta|Maltese", "NL": "Netherlands|Dutch", "PL": "Poland|Polish",
         "PT": "Portugal|Portuguese", "RO": "Romania|Romanian", "SE": "Sweden|Swedish",
         "SI": "Slovenia|Slovenian", "SK": "Slovakia|Slovak"}
WORD_RE = {c: re.compile(rf"\b({w})\b") for c, w in WORDS.items()}


def named_pairs(title):
    """Pairs of countries an English headline names by name or adjective.

    >>> named_pairs("German and Polish ministers meet"), named_pairs("Irish exports rise")
    (['DE-PL'], [])
    """
    found = sorted(c for c, rx in WORD_RE.items() if rx.search(title))
    return [pair_key(a, b) for i, a in enumerate(found) for b in found[i + 1:]]


def review(now, repeats, budget, rejected_max=100, misses_max=60):
    """Tag the last 3 days' candidates `repeats` times and write a labelling workbook: every
    (headline, pair) the checker confirmed in any run, a sample of proposals it always rejected (what
    the check may cost in recall), and unproposed English headlines that name two countries. Each
    row gets your_verdict (Y/N) and a note; the rows become calibration/pairs_gold.csv."""
    import random
    from openpyxl import Workbook
    from gdelt_gal import add_sheet

    records, _ = candidates(now, 3)
    by_id = {r["id"]: r for r in records}
    tagger, checker, v = prompts()
    meter = Meter(budget)
    both = [tag_and_check(records, tagger, checker, meter) for _ in range(repeats)]
    runs = [b[0] for b in both]
    hits = Counter((i, p) for run in runs for i, ps in run.items() for p in ps)
    proposals = Counter((i, p) for b in both for i, ps in b[1].items() for p in ps)
    reasons = {k: why for b in both for k, (why, _) in b[2].items()}
    tagged_ids = {i for i, _ in proposals}
    misses = [(r["id"], p) for r in records if r["id"] not in tagged_ids and r["lang"] == "en"
              for p in named_pairs(r["title"])][:misses_max]
    rejected = sorted(k for k in proposals if not hits[k])
    rejected = random.Random(0).sample(rejected, min(rejected_max, len(rejected)))
    rows = ([(i, p, hits[i, p], proposals[i, p], "confirmed") for i, p in hits]
            + [(i, p, 0, proposals[i, p], "rejected by checker") for i, p in rejected]
            + [(i, p, 0, 0, "possible miss") for i, p in misses])
    en = gloss([{"id": i, "title": by_id[i]["title"]} for i in {i for i, *_ in rows} if by_id[i]["lang"] != "en"], meter)
    english = lambda r: r["title"] if r["lang"] == "en" else en.get(r["id"], "")
    rows.sort(key=lambda x: (x[1], -x[2], -x[3], x[0]))

    out = ROOT / "exports" / f"pairs_review_{now:%Y%m%dT%H%MZ}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb = Workbook(write_only=True)
    add_sheet(wb, "Pairs", ["pair", "countries", "runs_confirmed", "runs_proposed", "sample", "title_en", "checker_reason",
                            "title", "source", "country", "lang", "relevance", "your_verdict", "your_note", "url", "id"],
              [[p, " and ".join(NAMES[c] for c in p.split("-")), f"{n}/{repeats}", f"{m}/{repeats}", kind,
                english(by_id[i]), reasons.get((i, p), ""), by_id[i]["title"], by_id[i]["source_name"],
                by_id[i]["source_country_raw"], by_id[i]["lang"], by_id[i]["relevance"], "", "", by_id[i]["url"], i]
               for i, p, n, m, kind in rows])
    feeds = Counter(r["feed_id"] for r in records)
    feed_tagged = Counter(by_id[i]["feed_id"] for i in {i for i, _ in hits})
    add_sheet(wb, "Sources", ["feed_id", "source", "candidates", "confirmed_in_any_run", "confirmed_pct"],
              [[f, next(r["source_name"] for r in records if r["feed_id"] == f), n, feed_tagged[f],
                round(100 * feed_tagged[f] / n, 1)] for f, n in feeds.most_common()])
    per_run = [sum(1 for ps in run.values() if ps) for run in runs]
    pairs_seen = Counter(p for _, p, n, m, kind in rows if n)
    ws = wb.create_sheet("Notes")
    for line in [f"Candidates: {len(records)} headlines with relevance >= {MIN_RELEVANCE}, 3 archive days to {now:%Y-%m-%d %H:%M} UTC",
                 f"Prompts {PROMPT_VERSION} + {CHECK_VERSION} (version {v}), {repeats} runs; headlines with a confirmed pair per run: {per_run}",
                 f"Rows: all {len(hits)} (headline, pair) confirmed in at least one run; {len(rejected)} of the "
                 f"{len(proposals) - len(hits)} proposals the checker always rejected (random sample); {len(misses)} possible "
                 f"misses (unproposed English headlines naming two countries, first {misses_max})",
                 f"Pairs confirmed: {len(pairs_seen)}; most: " + ", ".join(f"{p} {n}" for p, n in pairs_seen.most_common(12)),
                 "your_verdict: Y if the headline is about a relationship or interaction between the two countries, N if not; your_note for anything else",
                 f"Cost: ${meter.spent:.4f} for {meter.calls} calls (Haiku)"]:
        ws.append([line])
    wb.save(out)
    print(f"pairs review: {len(rows)} rows, {len(pairs_seen)} pairs, ${meter.spent:.4f}; wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", help="gold CSV (id, pair, expect, label, note, title, title_en, lang, country, source)")
    ap.add_argument("--review", action="store_true", help="local: write a labelling workbook from the last 3 days")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=float, default=RUN_BUDGET, help="USD cap for this run")
    a = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("pairs: OPENROUTER_API_KEY not set, skipping pair tagging")
        return
    if a.test:
        test(a.test, a.repeats)
        return
    now = datetime.now(timezone.utc)
    if a.review:
        review(now, a.repeats, max(a.budget, 0.30))
        return

    today = now.strftime("%Y-%m-%d")
    budget = min(a.budget, DAILY_BUDGET - spent_today(today))
    if budget <= 0:
        print(f"pairs: daily budget ${DAILY_BUDGET:.2f} reached, skipping")
        return
    tagger, checker, v = prompts()
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {}
    if progress.get("version") != v:      # new or edited prompt: tag the back-fill window again
        progress = {"version": v, "since": iso(now - timedelta(days=BACKFILL_DAYS)), "through": ""}

    records, latest = candidates(now, BACKFILL_DAYS + 1, progress["since"], progress["through"])
    meter = Meter(budget)
    try:
        got, proposed, verdicts, complete = tag_and_check(records, tagger, checker, meter)
    except InsufficientCredits as e:
        print(f"pairs: OpenRouter credits exhausted, stopping ({e})", file=sys.stderr)
        got, proposed, verdicts, complete = {}, {}, {}, False

    tagged_at = iso(datetime.now(timezone.utc))
    by_day = defaultdict(list)
    for r in records:
        if got.get(r["id"]):
            by_day[r["fetched_at"][:10]].append({"id": r["id"], "pairs": got[r["id"]],
                                                 "why": {p: verdicts[r["id"], p][0] for p in got[r["id"]]}, "version": v,
                                                 "fetched_at": r["fetched_at"], "tagged_at": tagged_at,
                                                 **{k: r.get(k) for k in DISPLAY}})
    for day, lines in by_day.items():
        path = PAIRTAGS / day[:4] / day[5:7] / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)
    if complete and latest:
        progress["through"] = latest
    PAIRTAGS.mkdir(exist_ok=True)
    PROGRESS.write_text(json.dumps(progress, indent=1) + "\n", encoding="utf-8")

    run = {"date": today, "run_at": tagged_at, "job": "pairs", "cost": round(meter.spent, 6), "calls": meter.calls,
           "checked": len(proposed), "proposed": sum(1 for x in proposed.values() if x),
           "tagged": sum(1 for x in got.values() if x), "pending": len(records),
           "complete": complete, "version": v}
    COSTS.parent.mkdir(exist_ok=True)
    with COSTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run) + "\n")
    print("pairs:", json.dumps(run))


if __name__ == "__main__":
    main()
