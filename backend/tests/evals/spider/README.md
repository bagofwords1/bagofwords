# Spider dataset

The eval script (`backend/tests/evals/spider_eval.py`) expects this layout:

```
backend/tests/evals/spider/
├── spider.json              # questions (list of {db_id, question, query})
└── database/
    ├── <db_id>/<db_id>.sqlite
    └── ...
```

Get the data either by:

1. Letting the script download it: `python spider_eval.py --download ...`
2. Or manually placing the official Spider release here. The standard release
   ships `dev.json` / `train_spider.json` and a `database/` folder — rename or
   symlink the questions file you want to evaluate to `spider.json`.

Each BoW data source must be registered ahead of time, one SQLite file per
`db_id`, with the data source **name matching `db_id` exactly** (the script
maps questions to data sources by name).
