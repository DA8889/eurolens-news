# Calibration: prompt gate_v7, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:09 UTC; headlines from calibration/gate_v2_fresh24h.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 86 (28.7%), 88 (29.3%), 83 (27.7%)
- **keep/drop flips between runs: 32 of 300 (10.7%)**
- **vs your labels, each run: 4/9, 6/9, 3/9**
- Run 1 vs your labels: **4/9**; wrong: Novi Sad memorial (5, you: keep); China AI bubble (5, you: keep); World stocks fall (4, you: keep); Burnham apology (5, you: keep); Felgueiras factory jobs (5, you: keep)
- cost $0.0160, 30 calls; 132 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v7_fresh24h.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
