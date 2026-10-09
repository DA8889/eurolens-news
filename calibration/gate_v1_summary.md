# Calibration: prompt gate_v1, anthropic/claude-haiku-5.5 vs anthropic/claude-sonnet-5.5
- sample: 300 headlines; scored by both: 300 (Haiku missed 0, Sonnet missed 0)
- **keep/drop agreement at relevance ≥6: 88.7%**
- relevance within ±1: 84.3%; mean |diff| 0.80; Haiku mean minus Sonnet mean +0.11
- significance within ±1: 89.3%
- kept (≥6): Haiku 89 (29.7%), Sonnet 67 (22.3%)
- Haiku: $0.0042, 10 calls, 95 input and 9.1 output tokens per headline, 0 reasoning tokens
- Sonnet (reference, reasoning low): $0.0815, 10 calls
- review file: calibration/gate_v1.csv (disagreements first; fill your_r / your_note)
