# Benchmark: z-ai/glm-5.3-flash (media: google/gemini-3.1-flash-lite), 2026-10-02

**12/14 passed** -- 107 calls, 1813372 prompt / 6420 completion tokens, 1650048 cached, $0.077557

| scenario | result | seconds | max turn s | prompt tok | cached | cost $ | problems |
|---|---|---|---|---|---|---|---|
| greeting_browse | PASS | 14.02 | 9.37 | 46388 | 43968 | 0.00179 |  |
| arabic_search | PASS | 11.28 | 11.28 | 30359 | 29312 | 0.001126 |  |
| image_match | PASS | 17.49 | 17.49 | 47164 | 44224 | 0.002152 |  |
| voice_note | PASS | 12.25 | 12.25 | 30464 | 29440 | 0.001211 |  |
| order_cairo_70 | PASS | 48.16 | 14.45 | 211307 | 207040 | 0.007115 |  |
| order_alex_85 | PASS | 37.72 | 13.81 | 157212 | 153536 | 0.005356 |  |
| exchange_return | PASS | 3.6 | 3.6 | 14720 | 14656 | 0.000499 |  |
| size_help | PASS | 22.86 | 13.92 | 77656 | 76096 | 0.00261 |  |
| unsold_garment | PASS | 11.31 | 11.31 | 30160 | 29312 | 0.001069 |  |
| drift_then_return | FAIL | 61.99 | 19.53 | 212355 | 201792 | 0.008081 | no order created |
| multi_item_cart_edit | PASS | 70.6 | 21.78 | 286739 | 267136 | 0.011336 |  |
| angry_customer | PASS | 6.29 | 6.29 | 14734 | 14720 | 0.000476 |  |
| ambiguous_short | PASS | 8.5 | 4.75 | 29463 | 29312 | 0.000945 |  |
| long_memory | FAIL | 156.25 | 23.98 | 624651 | 509504 | 0.033791 | tool confirm_order not called; no order created |
