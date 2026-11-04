"""Stage 1 calibration: score a sample of headlines with Haiku 5.5, several runs, for review.

    OPENROUTER_API_KEY=... python calibrate.py [--n 300] [--seed 7] [--hours 72]
        [--prompt gate_v4] [--sample-from calibration/gate_v1.csv] [--gold calibration/gold_v2.csv]
        [--name gate_v4_same300] [--repeats 3]

Local only. Samples archived headlines (stratified by language), scores them --repeats times
with the production model, and writes calibration/<name>.csv (and the same as .xlsx, plus a
Summary sheet, for review in Excel or LibreOffice) with an English gloss of each headline
(from a separate translation call, so the scoring prompt itself is untouched). Rows you
label by hand go into the gold set (spec §6).

Haiku 5.5 has no temperature setting, so its scores vary by run and one run can look better
or worse than the prompt is: judge a prompt on all runs. The review file shows run 1's scores
plus every run's relevance (haiku_r_runs), with headlines whose runs disagree on keep/drop first.

--sample-from rescores the exact headlines of an earlier calibration file (or the gold set),
so two prompts can be compared on the same items. --gold reports how often keep/drop matches
your hand labels, per run (calibration/gold_v2.csv: id, gold_keep Y/N, label, ...).
"""
import argparse
import csv
import json
import math
import random
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook

from collect import ROOT
from gdelt_gal import add_sheet
from score import (CHUNK, MODEL, PROMPT_VERSION, Meter, chat, line_for, load_prompt,
                   parse_json, read_days, score_chunk)

KEEP = 6                                  # "Most relevant" threshold
GLOSS_PROMPT = ('Translate each numbered news headline into short, plain English. '
                'Return only JSON: {"1": "...", "2": "..."} with one entry per line.')


def sample(items, n, rng):
    """Stratified by language: allocation proportional to sqrt(count), at least 3 each."""
    by_lang = defaultdict(list)
    for r in items:
        by_lang[r["lang"] or "?"].append(r)
    weights = {k: math.sqrt(len(v)) for k, v in by_lang.items()}
    total = sum(weights.values())
    picked = []
    for lang, group in by_lang.items():
        k = min(len(group), max(3, round(n * weights[lang] / total)))
        picked += rng.sample(group, k)
    rng.shuffle(picked)
    return picked[:n]


def run_model(records, system):
    meter = Meter(budget=2.0)
    chunks = [records[i:i + CHUNK] for i in range(0, len(records), CHUNK)]
    out = {}
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(lambda c: score_chunk(c, system, meter), chunks):
            out.update(got)
    return out, meter


def gloss(records):
    """English translations for human review only."""
    out = {}

    def one(chunk):
        user = "\n".join(f"{i} | {' '.join(r['title'].split())}" for i, r in enumerate(chunk, 1))
        content, _, _ = chat(MODEL, GLOSS_PROMPT, user, max_tokens=60 * len(chunk) + 100)
        try:
            data = parse_json(content)
        except (ValueError, json.JSONDecodeError):
            return {}
        return {r["id"]: data.get(str(i)) for i, r in enumerate(chunk, 1)}

    chunks = [records[i:i + CHUNK] for i in range(0, len(records), CHUNK)]
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(one, chunks):
            out.update(got)
    return out


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def gold_report(name, scores, gold):
    """Summary lines: how often keep (relevance >= KEEP) matches the hand labels."""
    hits, misses = 0, []
    for g in gold:
        rs = scores.get(g["id"])
        if rs is None:
            continue
        if (rs[0] >= KEEP) == (g["gold_keep"] == "Y"):
            hits += 1
        else:
            misses.append(f"{g['label']} ({rs[0]}, you: {'keep' if g['gold_keep'] == 'Y' else 'drop'})")
    n = hits + len(misses)
    return [f"- {name} vs your labels: **{hits}/{n}**" + (f"; wrong: {'; '.join(misses)}" if misses else "")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--hours", type=float, default=72, help="sample headlines first seen in the last N hours")
    ap.add_argument("--prompt", default=PROMPT_VERSION)
    ap.add_argument("--sample-from", help="rescore the headlines of an earlier calibration CSV")
    ap.add_argument("--gold", help="hand-labelled CSV (id, gold_keep, label)")
    ap.add_argument("--name", help="output file stem (default: the prompt name)")
    ap.add_argument("--repeats", type=int, default=3, help="Haiku runs (scores vary by run)")
    a = ap.parse_args()
    name = a.name or a.prompt

    now = datetime.now(timezone.utc)
    if a.sample_from:
        records = [{"id": x["id"], "title": x["title"], "lang": x["lang"] or None,
                    "source_country_raw": x["country"] or None, "source_name": x["source"] or None,
                    "fetched_at": None, "title_en": x["title_en"]} for x in read_csv(a.sample_from)]
        print(f"rescoring {len(records)} headlines from {a.sample_from}")
    else:
        since = now - timedelta(hours=a.hours)
        days = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(int(a.hours // 24) + 2)]
        items = [r for r in {r["id"]: r for r in read_days("archive", days)}.values()
                 if datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00")) >= since]
        records = sample(items, a.n, random.Random(a.seed))
        print(f"sampled {len(records)} of {len(items)} headlines first seen since {since:%Y-%m-%d %H:%M} UTC, "
              f"languages: {dict(Counter(r['lang'] for r in records).most_common())}")

    system = load_prompt(a.prompt)
    runs = [run_model(records, system) for _ in range(a.repeats)]
    haiku, hm = runs[0]
    en = {r["id"]: r["title_en"] for r in records if r.get("title_en")}
    en.update(gloss([r for r in records if not r.get("title_en")]))
    flipped = {r["id"] for r in records if len({sc[r["id"]][0] >= KEEP for sc, _ in runs if r["id"] in sc}) > 1}

    rows = []
    for r in records:
        h = haiku.get(r["id"])
        rows.append({"haiku_r": h and h[0], "haiku_s": h and h[1],
                     **({"haiku_r_runs": " ".join(str(sc[r["id"]][0]) if r["id"] in sc else "-" for sc, _ in runs)}
                        if a.repeats > 1 else {}),
                     "your_r": "", "your_note": "",
                     "lang": r["lang"], "country": r["source_country_raw"], "source": r["source_name"],
                     "title_en": en.get(r["id"]) or "", "title": r["title"], "id": r["id"]})
    rows.sort(key=lambda x: (x["id"] not in flipped, -(x["haiku_r"] if x["haiku_r"] is not None else -1)))

    out_dir = ROOT / "calibration"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    gold = read_csv(a.gold) if a.gold else []
    gold_keep = {g["id"]: g["gold_keep"] == "Y" for g in gold}
    n = len(records)
    seen = sorted(r["fetched_at"] for r in records if r["fetched_at"])
    kept = [sum(v[0] >= KEEP for v in sc.values()) for sc, _ in runs]
    summary = "\n".join([
        f"# Calibration: prompt {a.prompt}, {MODEL}, {a.repeats} run{'s' if a.repeats > 1 else ''}",
        f"- run at {now:%Y-%m-%d %H:%M} UTC; "
        + (f"headlines from {a.sample_from}" if a.sample_from else f"headlines first seen {seen[0]} to {seen[-1]}"),
        f"- sample: {n} headlines; unscored per run: {', '.join(str(n - len(sc)) for sc, _ in runs)}",
        f"- kept (relevance ≥{KEEP}) per run: {', '.join(f'{k} ({100 * k / n:.1f}%)' for k in kept)}",
        *([f"- **keep/drop flips between runs: {len(flipped)} of {n} ({100 * len(flipped) / n:.1f}%)**"]
          if a.repeats > 1 else []),
        *([f"- **vs your labels, each run: " + ", ".join(
            f"{sum((sc[i][0] >= KEEP) == k for i, k in gold_keep.items() if i in sc)}/{sum(i in sc for i in gold_keep)}"
            for sc, _ in runs) + "**"] + gold_report("Run 1", haiku, gold) if gold else []),
        f"- cost ${sum(m.spent for _, m in runs):.4f}, {sum(m.calls for _, m in runs)} calls; "
        f"{hm.tokens_in / max(len(haiku), 1):.0f} input and {hm.tokens_out / max(len(haiku), 1):.1f} output "
        f"tokens per headline, {sum(m.tokens_reasoning for _, m in runs)} reasoning tokens",
        f"- review file: {path.relative_to(ROOT)} and .xlsx (runs that disagree on keep/drop first; "
        f"fill your_r / your_note)",
    ])
    (out_dir / f"{name}_summary.md").write_text(summary + "\n", encoding="utf-8")
    wb = Workbook(write_only=True)
    add_sheet(wb, "Review", list(rows[0]), [list(x.values()) for x in rows])
    ws = wb.create_sheet("Summary")
    for line in summary.splitlines():
        ws.append([line.lstrip("#- ").replace("**", "")])
    wb.save(path.with_suffix(".xlsx"))
    print(summary)


if __name__ == "__main__":
    main()
