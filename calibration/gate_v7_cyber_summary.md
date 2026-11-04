# Calibration: prompt gate_v7, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:09 UTC; headlines from calibration/gate_v3_cyber.csv
- sample: 346 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 245 (70.8%), 248 (71.7%), 255 (73.7%)
- **keep/drop flips between runs: 43 of 346 (12.4%)**
- **vs your labels, each run: 2/3, 2/3, 2/3**
- Run 1 vs your labels: **2/3**; wrong: SPAR Obourg hack (5, you: keep)
- cost $0.0190, 36 calls; 137 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v7_cyber.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
