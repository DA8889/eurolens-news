# Calibration: prompt gate_v3, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:55 UTC; headlines from calibration/gate_v3_cyber.csv
- sample: 346 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 268 (77.5%), 261 (75.4%), 247 (71.4%)
- **keep/drop flips between runs: 37 of 346 (10.7%)**
- **vs your labels, each run: 3/3, 2/3, 2/3**
- Run 1 vs your labels: **3/3**
- cost $0.0181, 36 calls; 129 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v3_cyber_r3.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
