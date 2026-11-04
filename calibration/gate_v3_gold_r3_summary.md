# Calibration: prompt gate_v3, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:54 UTC; headlines from calibration/gold_v2.csv
- sample: 41 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 26 (63.4%), 30 (73.2%), 27 (65.9%)
- **keep/drop flips between runs: 7 of 41 (17.1%)**
- **vs your labels, each run: 27/41, 31/41, 32/41**
- Run 1 vs your labels: **27/41**; wrong: Spanish eviction (1) (4, you: keep); Paramount-Warner (6, you: drop); Slovak police pay (6, you: drop); Premier League shirts (5, you: keep); Plague case (4, you: keep); Flemish cuts (5, you: keep); Italy-Cyprus forum (5, you: keep); Spanish eviction (2) (4, you: keep); Vietnam Western Sahara (7, you: drop); Anthropic IPO (4, you: keep); Novi Sad memorial (4, you: keep); China AI bubble (5, you: keep); World stocks fall (4, you: keep); SPAR Obourg hack (5, you: keep)
- cost $0.0024, 6 calls; 152 input and 9.2 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v3_gold_r3.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
