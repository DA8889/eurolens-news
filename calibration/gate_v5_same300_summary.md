# Calibration: prompt gate_v5, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:57 UTC; headlines from calibration/gate_v1.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 75 (25.0%), 80 (26.7%), 79 (26.3%)
- **keep/drop flips between runs: 31 of 300 (10.3%)**
- **vs your labels, each run: 24/31, 24/31, 24/31**
- Run 1 vs your labels: **24/31**; wrong: Spanish eviction (1) (2, you: keep); Premier League shirts (4, you: keep); Kosovo president (5, you: keep); Plague case (2, you: keep); Flemish cuts (4, you: keep); Italy-Cyprus forum (5, you: keep); Spanish eviction (2) (2, you: keep)
- cost $0.0162, 30 calls; 134 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v5_same300.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
