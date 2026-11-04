# Calibration: prompt gate_v3, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:55 UTC; headlines from calibration/gate_v2_fresh24h.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 86 (28.7%), 93 (31.0%), 84 (28.0%)
- **keep/drop flips between runs: 32 of 300 (10.7%)**
- **vs your labels, each run: 0/7, 3/7, 2/7**
- Run 1 vs your labels: **0/7**; wrong: Vietnam Western Sahara (6, you: drop); Anthropic IPO (5, you: keep); Novi Sad memorial (5, you: keep); Bulgarian election poll (4, you: keep); China AI bubble (4, you: keep); World stocks fall (5, you: keep); Burnham apology (5, you: keep)
- cost $0.0152, 30 calls; 124 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v3_fresh24h_r3.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
