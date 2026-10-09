"""Stage 1 calibration: does Haiku 5.5 score headlines like Sonnet 5.5, under prompt P1?

    OPENROUTER_API_KEY=... python calibrate.py [--n 300] [--seed 7]

Local only. Samples archived headlines (stratified by language), scores them with the
production model and a stronger reference model using the identical prompt, and writes
calibration/<prompt>.csv with disagreements first and an English gloss of each headline
(from a separate translation call, so the scoring prompt itself is untouched). Rows you
correct by hand become the start of the gold set (spec §6).
"""
import argparse
import csv
import json
import math
import random
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from collect import ROOT
from score import (CHUNK, MODEL, PROMPT_VERSION, Meter, chat, line_for, load_prompt,
                   parse_json, read_days, score_chunk)

REFERENCE = "anthropic/claude-sonnet-5.5"
REFERENCE_REASONING = {"effort": "low"}   # Sonnet 5.5 cannot switch reasoning off
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


def run_model(records, model, reasoning=None):
    meter = Meter(budget=2.0)
    chunks = [records[i:i + CHUNK] for i in range(0, len(records), CHUNK)]
    system = load_prompt()
    out = {}
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(lambda c: score_chunk(c, system, meter, model, reasoning=reasoning), chunks):
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]
    items = list({r["id"]: r for r in read_days("archive", days)}.values())
    records = sample(items, a.n, random.Random(a.seed))
    print(f"sampled {len(records)} of {len(items)} headlines, languages: "
          f"{dict(Counter(r['lang'] for r in records).most_common())}")

    haiku, hm = run_model(records, MODEL)
    sonnet, sm = run_model(records, REFERENCE, REFERENCE_REASONING)
    en = gloss(records)

    rows = []
    for r in records:
        h, s = haiku.get(r["id"]), sonnet.get(r["id"])
        row = {"keep_agree": None, "r_diff": None,
               "haiku_r": h and h[0], "sonnet_r": s and s[0],
               "haiku_s": h and h[1], "sonnet_s": s and s[1],
               "your_r": "", "your_note": "",
               "lang": r["lang"], "country": r["source_country_raw"], "source": r["source_name"],
               "title_en": en.get(r["id"]) or "", "title": r["title"], "id": r["id"]}
        if h and s:
            row["keep_agree"] = "Y" if (h[0] >= KEEP) == (s[0] >= KEEP) else "N"
            row["r_diff"] = h[0] - s[0]
        rows.append(row)
    rows.sort(key=lambda x: (x["keep_agree"] != "N", -abs(x["r_diff"] or 0)))

    out_dir = ROOT / "calibration"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{PROMPT_VERSION}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    both = [x for x in rows if x["keep_agree"]]
    n = len(both)
    pct = lambda k: f"{100 * k / n:.1f}%" if n else "n/a"
    hk = sum(x["haiku_r"] >= KEEP for x in both)
    sk = sum(x["sonnet_r"] >= KEEP for x in both)
    summary = "\n".join([
        f"# Calibration: prompt {PROMPT_VERSION}, {MODEL} vs {REFERENCE}",
        f"- sample: {len(records)} headlines; scored by both: {n} "
        f"(Haiku missed {len(records) - len(haiku)}, Sonnet missed {len(records) - len(sonnet)})",
        f"- **keep/drop agreement at relevance ≥{KEEP}: {pct(sum(x['keep_agree'] == 'Y' for x in both))}**",
        f"- relevance within ±1: {pct(sum(abs(x['r_diff']) <= 1 for x in both))}; "
        f"mean |diff| {sum(abs(x['r_diff']) for x in both) / max(n, 1):.2f}; "
        f"Haiku mean minus Sonnet mean {sum(x['r_diff'] for x in both) / max(n, 1):+.2f}",
        f"- significance within ±1: {pct(sum(abs(x['haiku_s'] - x['sonnet_s']) <= 1 for x in both))}",
        f"- kept (≥{KEEP}): Haiku {hk} ({pct(hk)}), Sonnet {sk} ({pct(sk)})",
        f"- Haiku: ${hm.spent:.4f}, {hm.calls} calls, {hm.tokens_in / max(len(haiku), 1):.0f} input and "
        f"{hm.tokens_out / max(len(haiku), 1):.1f} output tokens per headline, "
        f"{hm.tokens_reasoning} reasoning tokens",
        f"- Sonnet (reference, reasoning low): ${sm.spent:.4f}, {sm.calls} calls",
        f"- review file: {path.relative_to(ROOT)} (disagreements first; fill your_r / your_note)",
    ])
    (out_dir / f"{PROMPT_VERSION}_summary.md").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
