# Calibration: prompt gate_v6, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:08 UTC; headlines from calibration/gate_v2_fresh24h.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 86 (28.7%), 86 (28.7%), 83 (27.7%)
- **keep/drop flips between runs: 29 of 300 (9.7%)**
- **vs your labels, each run: 4/9, 4/9, 3/9**
- Run 1 vs your labels: **4/9**; wrong: Vietnam Western Sahara (6, you: drop); Bulgarian election poll (5, you: keep); Burnham apology (5, you: keep); Felgueiras factory jobs (5, you: keep); Game industry jobs (5, you: keep)
- cost $0.0157, 30 calls; 129 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v6_fresh24h.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
