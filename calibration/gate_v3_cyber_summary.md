# Calibration: prompt gate_v3, anthropic/claude-haiku-5.5 vs anthropic/claude-sonnet-5.5
- run at 2026-11-03 15:27 UTC; headlines from /tmp/claude-1000/-home-eurolens-eurolens/f2470106-cbcc-4dda-b5f3-c4a3a71c0a39/scratchpad/cyber.csv
- sample: 346 headlines; scored by both: 346 (Haiku missed 0, Sonnet missed 0)
- **keep/drop agreement at relevance ≥6: 90.2%**
- relevance within ±1: 86.7%; mean |diff| 0.84; Haiku mean minus Sonnet mean +0.07
- significance within ±1: 84.7%
- kept (≥6): Haiku 255 (73.7%), Sonnet 233 (67.3%)
- Haiku: $0.0060, 12 calls, 129 input and 9.1 output tokens per headline, 0 reasoning tokens
- Sonnet (reference, reasoning low): $0.1172, 12 calls
- review file: calibration/gate_v3_cyber.csv and .xlsx (disagreements first; fill your_r / your_note)
