# Calibration: prompt gate_v7, anthropic/claude-haiku-5.5, 3 runs
- run at 2026-11-04 21:09 UTC; headlines from calibration/gate_v1.csv
- sample: 300 headlines; unscored per run: 0, 0, 0
- kept (relevance ≥6) per run: 80 (26.7%), 73 (24.3%), 88 (29.3%)
- **keep/drop flips between runs: 41 of 300 (13.7%)**
- **vs your labels, each run: 22/33, 22/33, 26/33**
- Run 1 vs your labels: **22/33**; wrong: Mecca pact/Houthis (4, you: keep); Spanish eviction (1) (4, you: keep); Slovak police pay (6, you: drop); Premier League shirts (4, you: keep); Plague case (4, you: keep); Italy-Cyprus forum (4, you: keep); Spanish eviction (2) (3, you: keep); Meloni electoral reform (4, you: keep); Bolsonaro protest (7, you: drop); Sensofusion listing (5, you: keep); Estonian explosives factory (5, you: keep)
- cost $0.0159, 30 calls; 132 input and 9.1 output tokens per headline, 0 reasoning tokens
- review file: calibration/gate_v7_same300.csv and .xlsx (runs that disagree on keep/drop first; fill your_r / your_note)
