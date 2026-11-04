"""Filter and score a GDELT export, and write the one workbook for the day.

    OPENROUTER_API_KEY=... python gdelt_score.py exports/gdelt_gal_<start>_24h.jsonl.gz [--budget 2.00]

Runs the free filters (gdelt_filter.py, rules in gdelt_filters.yaml), then scores every
kept article's headline with the production prompt, 30 per call. Removed articles are not
scored. An article the model fails to score stays blank, never 0. Scoring stops at --budget.

Output (git-ignored):
    exports/<export>_scored.xlsx   sheets: Top 100, Scored, Removed, Domains, Countries, Summary
                                   (every raw article is on Scored or Removed)
"""
import argparse
import gzip
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openpyxl import Workbook

from gdelt_filter import COLUMNS, ROOT, filter_articles, load_rules, row
from gdelt_gal import add_sheet
from score import (CHUNK, MODEL, PROMPT_VERSION, WORKERS, InsufficientCredits, Meter, load_prompt,
                   score_chunk)

KEEP = 6    # "relevant" = relevance >= 6, same as the "Most relevant" view
TOP = 100
SCORED = ["relevance", "significance", "copies", "published", "source_country", "outlet", "domain",
          "language", "title", "description", "url"]


def scoring_record(r):
    """GDELT article -> the record shape score.line_for expects (headline only)."""
    return {"id": r["url"], "title": r["title"], "lang": r.get("lang"),
            "source_country_raw": r.get("source_country"), "source_name": r.get("outletName")}


def haiku_scores(articles, budget):
    """{url: (relevance, significance)} from Haiku with the production prompt."""
    system, meter = load_prompt(PROMPT_VERSION), Meter(budget)
    records = [scoring_record(r) for r in articles]
    chunks = [records[i:i + CHUNK] for i in range(0, len(records), CHUNK)]
    out = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for got in pool.map(lambda c: score_chunk(c, system, meter), chunks):
            out.update(got)
    return out, meter


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", type=Path, help="exports/gdelt_gal_*.jsonl.gz from gdelt_gal.py")
    ap.add_argument("--rules", type=Path, default=ROOT / "gdelt_filters.yaml")
    ap.add_argument("--budget", type=float, default=2.00, help="USD cap for this run")
    a = ap.parse_args()

    with gzip.open(a.export, "rt", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    kept, removed, window_start, window_end, cutoff = filter_articles(records, load_rules(a.rules))
    stem = a.export.name.removesuffix(".jsonl.gz")
    download_path = a.export.with_name(f"{stem}_download.json")
    download = json.loads(download_path.read_text(encoding="utf-8")) if download_path.exists() else {}
    print(f"{len(records)} articles, {len(kept)} kept by the free filters; "
          f"scoring them with {MODEL} ({PROMPT_VERSION}), budget ${a.budget:.2f}")

    t0 = time.monotonic()
    try:
        scores, meter = haiku_scores(kept, a.budget)
    except InsufficientCredits as e:
        raise SystemExit(f"OpenRouter credits exhausted: {e}")
    score_s = time.monotonic() - t0

    scored = []
    for r in kept:
        rel, sig = scores.get(r["url"], (None, None))
        scored.append([rel, sig, r["copies"]] + row(r))
    scored.sort(key=lambda x: (x[0] is None, -(x[0] or 0), -(x[1] or 0), -x[2]))
    relevant = [x for x in scored if x[0] is not None and x[0] >= KEEP]
    top = sorted(relevant, key=lambda x: (-(x[0] + (x[1] or 0)), -x[2], -x[0]))[:TOP]
    unscored = sum(1 for x in scored if x[0] is None)

    reasons = Counter(why for _, why, _ in removed)
    domains = defaultdict(lambda: {"total": 0, "kept": 0, "relevant": 0, "reasons": Counter(), "samples": []})
    countries = defaultdict(lambda: {"total": 0, "kept": 0, "relevant": 0})
    for r in records:
        d = domains[r.get("domain")]
        d["total"] += 1
        countries[r.get("source_country")]["total"] += 1
        if len(d["samples"]) < 3:
            d["samples"].append(r.get("title") or "")
    for r in kept:
        is_rel = scores.get(r["url"], (-1, None))[0] >= KEEP
        for d in (domains[r.get("domain")], countries[r.get("source_country")]):
            d["kept"] += 1
            d["relevant"] += is_rel
    for r, why, _ in removed:
        domains[r.get("domain")]["reasons"][why] += 1

    out = a.export.with_name(f"{stem}_scored.xlsx")
    wb = Workbook(write_only=True)
    add_sheet(wb, "Top 100", SCORED, top)
    add_sheet(wb, "Scored", SCORED, scored)
    add_sheet(wb, "Removed", ["reason", "rule"] + COLUMNS,
              [[why, rule] + row(r) for r, why, rule in sorted(removed, key=lambda x: x[1])])
    add_sheet(wb, "Domains", ["domain", "articles", "kept", "removed", "main_removal_reason",
                              "relevant", "sample_1", "sample_2", "sample_3"],
              [[dom, d["total"], d["kept"], d["total"] - d["kept"],
                d["reasons"].most_common(1)[0][0] if d["reasons"] else "", d["relevant"], *d["samples"]]
               for dom, d in sorted(domains.items(), key=lambda kv: -kv[1]["total"])])
    add_sheet(wb, "Countries", ["source_country", "articles", "kept", "relevant", "relevant_per_100_kept"],
              [[cc, c["total"], c["kept"], c["relevant"],
                round(100 * c["relevant"] / c["kept"], 1) if c["kept"] else None]
               for cc, c in sorted(countries.items(), key=lambda kv: -kv[1]["relevant"])])
    n = len(records)
    ws = wb.create_sheet("Summary")
    for line in [
        ["input", a.export.name], ["window_start_utc", f"{window_start:%Y-%m-%d %H:%M}"],
        ["window_end_utc", f"{window_end:%Y-%m-%d %H:%M}"], ["rules", a.rules.name],
        ["old_cutoff_utc", f"{cutoff:%Y-%m-%d %H:%M}"], ["scoring_model", MODEL], ["prompt", PROMPT_VERSION],
        [], ["download (gdelt_gal.py)"], *[[k, v] for k, v in download.items()],
        [], ["step", "articles", "share_of_raw"], ["raw European articles", n, 1.0],
        *[[f"removed: {why}", c, round(c / n, 4)] for why, c in reasons.most_common()],
        ["kept (scored)", len(kept), round(len(kept) / n, 4)],
        [f"relevant (relevance >= {KEEP})", len(relevant), round(len(relevant) / n, 4)],
        [], ["check: Scored + Removed = raw", len(kept) + len(removed) == n],
        ["unscored (model failed or budget hit)", unscored],
        ["scoring_cost_usd", round(meter.spent, 4)], ["calls", meter.calls], ["seconds", round(score_s)],
        ["input_tokens_per_headline", round(meter.tokens_in / max(len(scores), 1), 1)],
        ["output_tokens_per_headline", round(meter.tokens_out / max(len(scores), 1), 1)],
        ["budget_usd", a.budget], ["budget_hit", meter.exhausted()],
    ]:
        ws.append(line)
    wb.save(out)

    print(f"window {window_start:%Y-%m-%d %H:%M} to {window_end:%Y-%m-%d %H:%M} UTC; raw {n} -> "
          f"kept {len(kept)} -> relevant {len(relevant)}; unscored {unscored}; "
          f"Scored + Removed = raw: {len(kept) + len(removed) == n}")
    print(f"cost ${meter.spent:.4f}, {meter.calls} calls, {score_s:.0f}s; budget hit: {meter.exhausted()}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
