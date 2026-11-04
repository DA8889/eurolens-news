# Calibration: prompt gate_v4, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:54 UTC; headlines from calibration/gold_v2.csv
- sample: 41 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 28 (68.3%), 29 (70.7%), 27 (65.9%)
- **keep/drop flips between runs: 10 of 41 (24.4%)**
- **vs your labels, each run: 31/41, 30/41, 32/41**
- Run 1 vs your labels: **31/41**; wrong: Mecca pact/Houthis (5, you: keep); Spanish eviction (1) (2, you: keep); Slovak police pay (6, you: drop); Premier League shirts (5, you: keep); Italy-Cyprus forum (5, you: keep); Spanish eviction (2) (2, you: keep); Vietnam Western Sahara (6, you: drop); Novi Sad memorial (5, you: keep); Binance hack (4, you: keep); SPAR Obourg hack (4, you: keep)
- cost $0.0026, 6 calls; 165 input and 9.2 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v4_gold.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
