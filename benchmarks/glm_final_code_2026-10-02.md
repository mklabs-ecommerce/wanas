# Benchmark: z-ai/glm-5.3-flash (media: google/gemini-3.1-flash-lite), 2026-10-02

**13/14 passed** -- 108 calls, 1871756 prompt / 6506 completion tokens, 1677376 cached, $0.083078

| scenario | result | seconds | max turn s | prompt tok | cached | cost $ | problems |
|---|---|---|---|---|---|---|---|
| greeting_browse | PASS | 19.87 | 13.24 | 47370 | 14912 | 0.005416 |  |
| arabic_search | PASS | 12.55 | 12.55 | 31007 | 29888 | 0.00115 |  |
| image_match | PASS | 18.09 | 18.09 | 32365 | 30016 | 0.001651 |  |
| voice_note | PASS | 14.33 | 14.33 | 30613 | 30016 | 0.001136 |  |
| order_cairo_70 | PASS | 81.16 | 22.61 | 232742 | 226112 | 0.008076 |  |
| order_alex_85 | PASS | 52.58 | 22.35 | 144026 | 140480 | 0.004945 |  |
| exchange_return | PASS | 7.38 | 7.38 | 15045 | 14976 | 0.00052 |  |
| size_help | PASS | 29.16 | 17.17 | 79286 | 77760 | 0.002671 |  |
| unsold_garment | PASS | 27.65 | 27.65 | 62623 | 59904 | 0.00241 |  |
| drift_then_return | PASS | 56.33 | 24.1 | 147706 | 143168 | 0.005228 |  |
| multi_item_cart_edit | PASS | 101.95 | 41.14 | 354934 | 323520 | 0.014945 |  |
| angry_customer | PASS | 3.76 | 3.76 | 15059 | 14976 | 0.000507 |  |
| ambiguous_short | PASS | 9.53 | 6.27 | 30114 | 29952 | 0.00098 |  |
| long_memory | FAIL | 174.96 | 23.62 | 648866 | 541696 | 0.033443 | no order created |
