| Agent | Metric | Base, 20 rows | Base, 300k rows | Branch, 20 rows | Branch, 100k rows | Branch, 300k rows |
|---|---|---|---|---|---|---|
| Wide Postgres | schema build (ms) | 551.5 (552/494/602) | 477.5 (478/642/453) | 553.6 (544/614/554) | 575.4 (535/637/575) | 652.3 (598/664/652) |
| Wide Postgres | planner render (ms) | 17.2 (22/14/17) | 15.2 (15/27/15) | 16.4 (14/16/22) | 15.0 (15/17/15) | 15.8 (14/16/16) |
| Wide Postgres | planner prep, end to end (ms) | 530.6 (475/549/531) | 598.9 (475/603/599) | 529.2 (529/752/513) | 562.6 (546/591/563) | 632.7 (548/633/634) |
| Wide Postgres | SQL statements per build | – | – | 20 | 20 | 20 |
| Wide DuckDB | schema build (ms) | 67.6 (77/68/60) | 56.4 (54/56/60) | 94.8 (92/101/95) | 142.5 (100/142/147) | 103.0 (103/132/99) |
| Wide DuckDB | planner render (ms) | 10.6 (16/11/10) | 9.3 (9/9/9) | 7.6 (8/8/7) | 13.3 (7/13/14) | 7.5 (7/12/8) |
| Wide DuckDB | planner prep, end to end (ms) | 72.4 (72/72/105) | 70.1 (70/117/70) | 114.9 (129/115/112) | 166.3 (119/188/166) | 126.2 (126/153/122) |
| Wide DuckDB | SQL statements per build | – | – | 17 | 17 | 17 |
| Wide Elastic | schema build (ms) | 49.8 (50/47/63) | 64.7 (65/52/65) | 79.8 (80/104/73) | 131.1 (117/131/155) | 101.9 (102/100/111) |
| Wide Elastic | planner render (ms) | 6.9 (7/6/7) | 9.0 (10/6/9) | 4.6 (5/5/5) | 8.5 (6/8/9) | 5.4 (5/5/6) |
| Wide Elastic | planner prep, end to end (ms) | 69.8 (60/75/70) | 66.6 (84/67/59) | 94.3 (85/108/94) | 152.7 (97/153/170) | 108.1 (138/108/108) |
| Wide Elastic | SQL statements per build | – | – | 17 | 17 | 17 |

rows: {'perf0': 23, 'perf100k': 100002, 'perf300k': 300000}
| Agent | per-step write, 20 rows | 100k rows | 300k rows | columns written per step |
|---|---|---|---|---|
| Wide Postgres | 46.8 ms | 47.1 ms | 51.5 ms | 5 |
| Wide DuckDB | 24.1 ms | 29.3 ms | 26.0 ms | 4 |
| Wide Elastic | 24.0 ms | 29.9 ms | 30.3 ms | 3 |
