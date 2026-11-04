# Calibration: prompt gate_v6, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:08 UTC; headlines from calibration/gate_v3_cyber.csv
- sample: 346 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 255 (73.7%), 264 (76.3%), 259 (74.9%)
- **keep/drop flips between runs: 27 of 346 (7.8%)**
- **vs your labels, each run: 2/3, 2/3, 2/3**
- Run 1 vs your labels: **2/3**; wrong: SPAR Obourg hack (5, you: keep)
- cost $0.0187, 36 calls; 134 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v6_cyber.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
