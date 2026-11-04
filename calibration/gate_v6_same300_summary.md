# Calibration: prompt gate_v6, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:08 UTC; headlines from calibration/gate_v1.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 76 (25.3%), 74 (24.7%), 76 (25.3%)
- **keep/drop flips between runs: 26 of 300 (8.7%)**
- **vs your labels, each run: 23/33, 25/33, 22/33**
- Run 1 vs your labels: **23/33**; wrong: Spanish eviction (1) (3, you: keep); Paramount-Warner (6, you: drop); Slovak police pay (6, you: drop); Premier League shirts (4, you: keep); Plague case (3, you: keep); Flemish cuts (5, you: keep); Italy-Cyprus forum (5, you: keep); Spanish eviction (2) (4, you: keep); Sensofusion listing (4, you: keep); Estonian explosives factory (4, you: keep)
- cost $0.0157, 30 calls; 129 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v6_same300.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
