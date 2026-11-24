# Examples

Everything in this folder is written by `python -I make_examples.py`, and nothing in it is edited by hand: CI writes it again and fails if git then shows a difference.

It is the real output of `python -I demo.py --case happy-path --as-of 2026-11-24 --at 2026-11-24T09:00:00Z` on the shipped sheet (`make_sheet.py`, seed 7, 25 customers) with `recorded-answer.json`: one real answer from `claude-haiku-5-5`, recorded on 2026-11-24 (a Claude Code subagent given the request text as its whole prompt; the answer saved as returned).

What the checks did with it: 22 of 22 owing customers accepted, 0 item(s) rejected, 0 missing, and 0 note(s) for a person set aside.

The demo printed:

```
happy-path | expected: exit 0; the export holds only the approved drafts | happened: exit 0; 22 drafts; items 3 and 5 approved, item 1 rejected (a note names it), 19 pending; the export holds reminders reminder-03.txt, reminder-05.txt and call-list lines 3, 5; history lines 2; report written | PASS
```

| File | What it is |
|---|---|
| `call-list.csv` | The call list the run drafted, in priority order, with the (invented) names and each customer's ageing bucket |
| `reminders/` | The first 3 of the run's 22 reminder drafts |
| `for-a-person.txt` | The page for a person: the model's notes, what the checks refused, who is missing |
| `run-report.json` | The run report: stand-ins only, the ageing, and the sha256 of every draft as written |
| `decisions.json` | Items 3 and 5 approved, item 1 rejected with a reason; every other item is pending |
| `approved/` | The export: only the approved call-list lines and reminders |
| `history.jsonl` | The run history the export wrote: one line per approved customer, and no name |
| `report.html` | The report page, written after the decisions and the export |
