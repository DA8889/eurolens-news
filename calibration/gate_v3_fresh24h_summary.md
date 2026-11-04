# Calibration: prompt gate_v3, anthropic/claude-haiku-5.5 vs anthropic/claude-sonnet-5.5
- run at 2026-11-03 15:27 UTC; headlines from calibration/gate_v2_fresh24h.csv
- sample: 300 headlines; scored by both: 300 (Haiku missed 0, Sonnet missed 0)
- **keep/drop agreement at relevance ≥6: 92.3%**
- relevance within ±1: 88.7%; mean |diff| 0.68; Haiku mean minus Sonnet mean +0.17
- significance within ±1: 88.0%
- kept (≥6): Haiku 84 (28.0%), Sonnet 85 (28.3%)
- Haiku: $0.0051, 10 calls, 124 input and 9.1 output tokens per headline, 0 reasoning tokens
- Sonnet (reference, reasoning low): $0.0986, 10 calls
- review file: calibration/gate_v3_fresh24h.csv and .xlsx (disagreements first; fill your_r / your_note)
