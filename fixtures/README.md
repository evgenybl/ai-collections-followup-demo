# Fixtures

The first version of this demo shipped a six-row sheet, a prompt, and one answer to the request built from them. They
are kept here unchanged, so that the original tests in `test_pipeline.py` run on the data they were written for:

| File | What it is |
|---|---|
| `customers.six-rows.csv` | The original invented sheet: six rows, five invented customers, one of them on two rows |
| `prompt.six-rows.txt` | The prompt of that time, byte for byte; the shipped `prompt.txt` has changed since |
| `recorded-answer.six-rows.json` | By the author's statement, one answer from a Claude model to the request built from that sheet and that prompt, saved unedited |
| `recorded-answer.six-rows.meta.json` | Where that answer came from, as the author states it, and the sha256 of the request it answers; a test compares it with the request built today |

That the answer is a model's rests on the author's statement, which no file here can prove: the sha256 proves only
that the request it answers is the one these files build. The sheet the pipeline uses by default is now generated: see
`make_sheet.py` and `customers.synthetic.csv`.
