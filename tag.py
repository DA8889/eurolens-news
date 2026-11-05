"""Tag scored headlines with the strands you follow (strands.yaml), using Haiku.

    python tag.py                    tag the headlines scored since the last run
    python tag.py --test calibration/strands_gold.csv [--repeats 3]    local: check the prompt

Candidates are headlines with relevance >= MIN_RELEVANCE. Each call sends CHUNK of them with
the strand list (prompts/PROMPT_VERSION.txt); Haiku returns the strands each belongs to, and
most belong to none. Exact-duplicate titles are asked once. Only headlines with a strand are
written, to tags/YYYY/MM/<item's archive day>.jsonl:
    {"id", "strands", "version", "fetched_at", "tagged_at"}
tags/progress.json holds the strands version (a hash of the prompt and strands.yaml), the
back-fill start ("since") and the last scoring run fully tagged ("through"). Editing
strands.yaml changes the version, so the last BACKFILL_DAYS days are tagged again; older
lines keep their tags (collect.read_tags decides which line counts). If a call fails or the
budget runs out, "through" stays put and those headlines are retried next run, never taken
as "no strand". Costs go to scores/costs.jsonl, under score.py's daily budget.
In collect.yml this runs only when the repository variable SCORING is "on".
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import yaml

from collect import ARCHIVE, ROOT, SCORES, TAGS, build_site, iso, read_archive
from score import (COSTS, DAILY_BUDGET, MODEL, WORKERS, InsufficientCredits, Meter, chat, line_for,
                   parse_json, spent_today, title_key)

PROMPT_VERSION = "strands_v1"
MIN_RELEVANCE = 3      # below "Most relevant" (6): small European company news often scores 3-5
CHUNK = 40
RUN_BUDGET = 0.05      # USD per run; about $0.03 a day (~4,300 headlines at relevance 3+)
BACKFILL_DAYS = 3      # after a strands.yaml change; about $0.09
PROGRESS = TAGS / "progress.json"


def load_strands():
    return yaml.safe_load((ROOT / "strands.yaml").read_text(encoding="utf-8"))["strands"]


def system_prompt(strands):
    lines = []
    for s in strands:
        examples = "; ".join(f'"{e}"' for e in s.get("examples") or [])
        lines.append(f"- {s['id']}: {s['name']}. {' '.join(s['description'].split())}"
                     + (f" Examples: {examples}." if examples else ""))
    text = (ROOT / "prompts" / f"{PROMPT_VERSION}.txt").read_text(encoding="utf-8")
    return text.replace("{strands}", "\n".join(lines))


def version(system):
    return hashlib.sha1(system.encode("utf-8")).hexdigest()[:12]


def parse_tags(content, n, ids):
    """{line number: [strand ids]} for lines with at least one known strand.

    >>> parse_tags('{"1": ["companies", "x"], "2": "companies", "3": [], "9": ["companies"]}', 3, {"companies"})
    {1: ['companies']}
    """
    data = parse_json(content)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    out = {}
    for k, v in data.items():
        if k.isdigit() and 1 <= int(k) <= n and isinstance(v, list):
            got = list(dict.fromkeys(s for s in v if s in ids))
            if got:
                out[int(k)] = got
    return out


def tag_chunk(records, system, ids, meter):
    """{id: [strands]} for every record ([] = none), or None if the model failed twice."""
    user = "\n".join(line_for(i, r) for i, r in enumerate(records, 1))
    for _ in range(2):
        if meter.exhausted():
            return None
        content, finish, usage = chat(MODEL, system, user, 15 * len(records) + 60)
        meter.add(usage)
        if finish == "length":
            continue
        try:
            got = parse_tags(content, len(records), ids)
        except (ValueError, json.JSONDecodeError):
            continue
        return {r["id"]: got.get(i, []) for i, r in enumerate(records, 1)}
    return None


def tag_all(records, system, ids, meter):
    """({id: [strands]}, all chunks answered?) with exact-duplicate titles asked once."""
    groups = defaultdict(list)
    for r in records:
        groups[title_key(r["title"])].append(r)
    reps = [g[0] for g in groups.values()]
    chunks = [reps[i:i + CHUNK] for i in range(0, len(reps), CHUNK)]
    out, complete = {}, True
    with ThreadPoolExecutor(WORKERS) as pool:
        for got in pool.map(lambda c: tag_chunk(c, system, ids, meter), chunks):
            if got is None:
                complete = False
            else:
                out.update(got)
    for g in groups.values():
        if g[0]["id"] in out:
            out.update({r["id"]: out[g[0]["id"]] for r in g[1:]})
    return out, complete


def test(path, repeats):
    """Tag the gold rows `repeats` times; Y rows need their strand in most runs, N rows in none."""
    strands = load_strands()
    system, ids = system_prompt(strands), {s["id"] for s in strands}
    with open(path, newline="", encoding="utf-8-sig") as f:
        gold = list(csv.DictReader(f))
    records = list({g["id"]: {"id": g["id"], "title": g["title"], "lang": g["lang"] or None,
                              "source_country_raw": g["country"] or None, "source_name": g["source"] or None}
                    for g in gold}.values())
    meter = Meter(budget=0.20)
    runs = [tag_all(records, system, ids, meter)[0] for _ in range(repeats)]
    passed = 0
    for g in gold:
        hits = sum(1 for run in runs if (run.get(g["id"]) if g["strand"] == "any" else g["strand"] in run.get(g["id"], [])))
        ok = hits * 2 > repeats if g["expect"] == "Y" else hits == 0
        passed += ok
        print(f"{'pass' if ok else 'FAIL'}  {g['expect']} {g['strand']:<20} {hits}/{repeats}  {g['label']}"
              f"  [{'; '.join(' '.join(run.get(g['id'], [])) or '-' for run in runs)}]")
    print(f"{passed}/{len(gold)} rows pass; ${meter.spent:.4f} for {meter.calls} calls; version {version(system)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", help="gold CSV (id, strand, expect, label, title, lang, country, source)")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=float, default=RUN_BUDGET, help="USD cap for this run")
    a = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("tag: OPENROUTER_API_KEY not set, skipping tagging")
        return
    if a.test:
        test(a.test, a.repeats)
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    budget = min(a.budget, DAILY_BUDGET - spent_today(today))
    if budget <= 0:
        print(f"tag: daily budget ${DAILY_BUDGET:.2f} reached, skipping")
        return
    strands = load_strands()
    system, ids = system_prompt(strands), {s["id"] for s in strands}
    v = version(system)
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {}
    if progress.get("version") != v:      # new or edited strands: tag the back-fill window again
        progress = {"version": v, "since": iso(now - timedelta(days=BACKFILL_DAYS)), "through": ""}

    days = BACKFILL_DAYS + 1
    scores = {s["id"]: s for s in read_archive(now, days, SCORES)
              if s["relevance"] >= MIN_RELEVANCE and s["scored_at"] > progress["through"]}
    records = [r for r in read_archive(now, days, ARCHIVE)
               if r["id"] in scores and r["fetched_at"] >= progress["since"]]
    meter = Meter(budget)
    try:
        got, complete = tag_all(records, system, ids, meter)
    except InsufficientCredits as e:
        print(f"tag: OpenRouter credits exhausted, stopping ({e})", file=sys.stderr)
        got, complete = {}, False

    tagged_at = iso(datetime.now(timezone.utc))
    by_day = defaultdict(list)
    for r in records:
        if got.get(r["id"]):
            by_day[r["fetched_at"][:10]].append({"id": r["id"], "strands": got[r["id"]], "version": v,
                                                 "fetched_at": r["fetched_at"], "tagged_at": tagged_at})
    for day, lines in by_day.items():
        path = TAGS / day[:4] / day[5:7] / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(l) + "\n" for l in lines)
    if complete and records:
        progress["through"] = max(scores[r["id"]]["scored_at"] for r in records)
    TAGS.mkdir(exist_ok=True)
    PROGRESS.write_text(json.dumps(progress, indent=1) + "\n", encoding="utf-8")

    run = {"date": today, "run_at": tagged_at, "job": "tag", "cost": round(meter.spent, 6), "calls": meter.calls,
           "checked": len(got), "tagged": sum(1 for x in got.values() if x), "pending": len(records),
           "complete": complete, "version": v}
    with COSTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run) + "\n")
    print("tag:", json.dumps(run))
    build_site(now)


if __name__ == "__main__":
    main()
