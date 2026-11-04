# Calibration: prompt gate_v2, anthropic/claude-haiku-5.5 vs anthropic/claude-sonnet-5.5
- run at 2026-11-02 13:05 UTC; headlines first seen 2026-11-01T16:17:07Z to 2026-11-02T06:44:08Z
- sample: 300 headlines; scored by both: 300 (Haiku missed 0, Sonnet missed 0)
- **keep/drop agreement at relevance ≥6: 91.0%**
- relevance within ±1: 87.7%; mean |diff| 0.68; Haiku mean minus Sonnet mean +0.09
- significance within ±1: 86.3%
- kept (≥6): Haiku 88 (29.3%), Sonnet 87 (29.0%)
- Haiku: $0.0049, 10 calls, 117 input and 9.1 output tokens per headline, 0 reasoning tokens
- Sonnet (reference, reasoning low): $0.0947, 10 calls
- review file: calibration/gate_v2_fresh24h.csv (disagreements first; fill your_r / your_note)
