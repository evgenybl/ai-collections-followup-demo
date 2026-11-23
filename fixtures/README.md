# Fixtures

The first version of this demo shipped a six-row sheet and one real answer from a Claude model to it. They are kept
here unchanged, so that the original tests in `test_pipeline.py` run on the data they were written for:

| File | What it is |
|---|---|
| `customers.six-rows.csv` | The original invented sheet: six rows, five invented customers, one of them on two rows |
| `recorded-answer.six-rows.json` | One real answer from a Claude model to the request built from that sheet, saved unedited |
| `recorded-answer.six-rows.meta.json` | Where that answer came from, and the sha256 of the request it answers; a test compares it with the request built today |

The sheet the pipeline uses by default is now generated: see `make_sheet.py` and `customers.synthetic.csv`.
