# Calibration: prompt gate_v4, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 10:54 UTC; headlines from calibration/gate_v1.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 84 (28.0%), 76 (25.3%), 81 (27.0%)
- **keep/drop flips between runs: 30 of 300 (10.0%)**
- **vs your labels, each run: 22/31, 21/31, 23/31**
- Run 1 vs your labels: **22/31**; wrong: Spanish eviction (1) (3, you: keep); Paramount-Warner (6, you: drop); Premier League shirts (4, you: keep); Plague case (3, you: keep); Trump AI term ban (7, you: drop); Flemish cuts (5, you: keep); Spanish eviction (2) (4, you: keep); Kyiv's coffers (3, you: keep); Bolsonaro protest (6, you: drop)
- cost $0.0160, 30 calls; 132 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v4_same300.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
