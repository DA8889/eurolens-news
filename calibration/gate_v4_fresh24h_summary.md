# Calibration: prompt gate_v4, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:55 UTC; headlines from calibration/gate_v2_fresh24h.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 91 (30.3%), 91 (30.3%), 92 (30.7%)
- **keep/drop flips between runs: 29 of 300 (9.7%)**
- **vs your labels, each run: 7/7, 5/7, 6/7**
- Run 1 vs your labels: **7/7**
- cost $0.0160, 30 calls; 132 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v4_fresh24h.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
