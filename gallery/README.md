# The failure gallery

`demo.py` runs ten named cases and prints, for each one, what was expected, what happened, and PASS or FAIL. Four of
them read the files in this folder.

**Every file here is constructed, never recorded.** Each is the recorded model answer (`recorded-answer.json`) or the
shipped sheet (`customers.synthetic.csv`) with exactly one thing changed, as the table says. `python -I
make_examples.py` writes them again from those two files, and a test checks that the files here are what it writes.

| File | Made from | The one change |
|---|---|---|
| `wrong-total.json` | `recorded-answer.json` | The `amount` of the call-list item with priority 2 is raised by 100. The rest is as recorded, written out again as indented JSON. |
| `invented-customer.json` | `recorded-answer.json` | One call-list item is added at the end, for `Customer 026`: a stand-in one past the sheet's last customer, so the sheet cannot have it. It gets the next priority, an amount of 1000, 30 days late, a reason and a reminder. The rest is as recorded, written out again as indented JSON. |
| `bad-sheet.csv` | `customers.synthetic.csv` | The amount of the first row is written `4,200`. |
| `leak-sheet.csv` | `customers.synthetic.csv` | The note of the first row becomes "Ask", the first word of the customer's name, a line break, the rest of the name, and "to call back". The name, broken by the line break, gets past the scrub; the table joins the line; the leak guard finds the name in the request and refuses. |

## The cases

Each command writes a new folder `demo-NNN` under `out-demo` (git ignores `out-*` folders). Without `--case`, all ten
run. `python -I demo.py --list` lists them.

| Case | What it shows | Ends as | Command |
|---|---|---|---|
| `happy-path` | The recorded answer; a person approves the first two drafts the model left no note about and rejects the first one a note names; the export; the report | exit 0; the export holds only the approved drafts | `python -I demo.py --case happy-path --out out-demo` |
| `bad-sheet` | An amount written `4,200` (`bad-sheet.csv`) | refused before anything is sent, exit 2, nothing written | `python -I demo.py --case bad-sheet --out out-demo` |
| `wrong-total` | The model's total differs from the sheet's (`wrong-total.json`) | that item rejected with the reason; the others kept | `python -I demo.py --case wrong-total --out out-demo` |
| `invented-customer` | The model adds a customer who is not in the sheet (`invented-customer.json`) | rejected | `python -I demo.py --case invented-customer --out out-demo` |
| `leak-guard` | A personal value still in the request (`leak-sheet.csv`) | refused, exit 2, nothing sent | `python -I demo.py --case leak-guard --out out-demo` |
| `second-run` | The same sheet again inside the window, after every draft was approved and exported | nothing is drafted twice; the model is not called | `python -I demo.py --case second-run --out out-demo` |
| `no-approval` | An export with no decision | nothing exported | `python -I demo.py --case no-approval --out out-demo` |
| `foreign-decisions` | A decisions file copied from another run (one answered by `wrong-total.json`, so its drafts differ) | refused, exit 2 | `python -I demo.py --case foreign-decisions --out out-demo` |
| `api-error` | The live path against a mock on 127.0.0.1 that answers 500 | refused, exit 3, nothing written | `python -I demo.py --case api-error --out out-demo` |
| `webhook-down` | The webhook is a closed port on 127.0.0.1 | outputs kept, exit 4 | `python -I demo.py --case webhook-down --out out-demo` |

The four cases that read this folder are also one pipeline command each, for example
`python -I pipeline.py --recorded gallery/wrong-total.json --out out-wrong-total` or
`python -I pipeline.py --sheet gallery/bad-sheet.csv --out out-bad-sheet`.

## How a case is judged

- `wrong-total` and `invented-customer` are judged against what the recorded answer itself gives on the sheet: exactly
  one more rejection, of the right kind, and every other item kept as before.
- `second-run` approves and exports every draft of the first run, then runs again inside the window. It expects the
  recorded answer to draft every owing customer; if a recording leaves one out, that customer is asked about again and
  the case says FAIL with what happened.
- `api-error` sets a placeholder key in the environment for the length of the case, because the live path needs one,
  and puts back whatever was there before. The mock never reads it.
