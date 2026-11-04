# Calibration: prompt gate_v5, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:57 UTC; headlines from calibration/gate_v3_cyber.csv
- sample: 346 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 252 (72.8%), 244 (70.5%), 242 (69.9%)
- **keep/drop flips between runs: 38 of 346 (11.0%)**
- **vs your labels, each run: 3/3, 2/3, 1/3**
- Run 1 vs your labels: **3/3**
- cost $0.0192, 36 calls; 140 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v5_cyber.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
