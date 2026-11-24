# Design

How the demo is put together, and why it is put together that way. The README says what it does; `docs/TESTS.md`
lists what the tests prove.

## The parts

| File | What it does |
|---|---|
| `pipeline.py` | Checks the sheet, replaces the personal columns with stand-ins, reads the run history, asks the model (a recorded answer or the Messages API), checks every figure of the answer against the sheet, maps the stand-ins back, writes the drafts |
| `pseudonymise.py` | The stand-ins and the scrub of the notes, with a local key |
| `approval.py` | A person's decisions on the drafts, and the export of only what was approved |
| `report.py` | One self-contained HTML page about a run |
| `demo.py` | The failure gallery: ten named cases through the real command lines |
| `make_sheet.py` | The synthetic sheet, generated from a seed |
| `make_examples.py` | Writes `gallery/`, `examples/` and `docs/TESTS.md` again from their sources |
| `run_tests.py` | Runs every `test_*.py` file in one command |
| `prompt.txt`, `customers.synthetic.csv`, `recorded-answer.json` | The fixed prompt, the shipped sheet, and the model answer the demo replays, with its meta file |
| `fixtures/` | The first version's six-row sheet and the real model answer recorded for it, which the original tests still use |

## What a run leaves behind

```
<run folder>/                written by pipeline.py; it refuses a folder that exists
  call-list.csv              the accepted items in priority order, real names, with the ageing bucket
  reminders/reminder-NN.txt  one draft per accepted item, NN being its priority
  for-a-person.txt           notes, refusals and missing customers, real names
  for-a-person.json          the same, in a form report.py reads
  run-report.json            stand-ins only: counts, refusals, ageing, the sha256 of every draft as written
  decisions.json             written by approval.py approve and reject
  approved/                  written once by approval.py export: the approved call-list lines and reminders
<history file>               JSON lines, appended by approval.py export, read by the next pipeline.py run
```

## Decisions, and why

**The history is written at export, not when a draft is written.** A draft nobody approved was never used. If
drafting wrote the history, a run whose drafts were rejected or never looked at would keep those customers out of the
next run for the whole window, and they would silently not be followed up. Writing at export ties "already drafted"
to "a person approved it and it left the run folder". The cost is visible and harmless: two runs drafted before any
export draft the same customers twice, in two folders, and nothing is sent either way.

**Only the same total keeps a customer out.** If the total changed, the earlier reminder states a wrong figure now, so
the customer is drafted again and marked "amount changed since the last draft". Of several history lines for a
customer inside the window, the latest counts. A line dated after the run's date is refused rather than ignored or
read as old: either the date of the run or the history is wrong, and the tool does not guess which.

**Decisions are appended, never rewritten.** `decisions.json` is a record of who decided what and when. A changed mind
is a new entry, so the earlier decision stays visible, and the last entry for an item counts. The file is replaced
whole on each decision, through a temporary file, so that a stopped write cannot leave half a file; the tool only ever
adds an entry, and a test checks that an earlier entry is unchanged after a later one.

**Decisions name their run.** `decisions.json` holds the run's `request_sha256` and one sha256 over the sha256 of every
draft. Copied into another run's folder it is refused, so an approval can never land on drafts it was not given for.
Two runs with the same request and the same drafts have the same drafts, and their decisions are interchangeable; a
test shows that too.

**A changed draft is refused, not checked again.** The checks ran on what the model wrote. A draft edited by hand could
carry a figure nobody checked, so a decision, an export and the report all refuse a run whose draft no longer matches
its sha256. To change a draft, change the input and run again. This guards against accidents, not against someone who
also edits `run-report.json`.

**The gallery is constructed.** A model cannot be made to fail on cue, and a real failure would not come back the same
way twice. Each wrong answer is the recorded answer with one documented change, written again by `make_examples.py`
whenever the recording changes, and each case is judged against what the recorded answer itself gives. So the gallery
shows what the checks do with a given mistake; it says nothing about how often a model makes it.

**Ageing is derived when the sheet is read, never stored and never sent.** It comes from the largest delay among a
customer's open rows. The model is not told it, so the request, and the sha256 a recording is tied to, do not depend
on it, and the model cannot repeat a wrong bucket.

**The history key is a hash, and a hash of a name is not anonymous.** It is a sha256 over a fixed prefix, the name
(spaces collapsed, letter case ignored) and the identity numbers (spaces and hyphens removed). The history file holds
no name, but anyone who has the sheet can compute the same key, and a name can be guessed and tested against it.

**The sheet is generated from a seed with sha256, not with the random module.** Python promises to keep only
`random()` itself the same across versions, not `choice()`, `shuffle()` or `randrange()`. CI generates the sheet on
two Python versions and two systems and compares it byte for byte with the shipped one.

**Files are written with LF line ends on every system.** A draft's sha256 then does not depend on the system that wrote
it, so the examples can be compared byte for byte wherever they are written again. The call lists keep the csv
module's CRLF line ends, as they always had, and `.gitattributes` keeps those bytes in `examples/` as they are.

**The original tests keep their data.** The shipped sheet is now generated, so the six-row sheet and the real recording
made for it moved to `fixtures/`, and the original tests read them there with their assertions unchanged.

**There is still no send step.** Nothing in the repository sends anything to a customer. `sent_to_customers` is 0 in
every run report, and a test keeps it there.

## Exit codes

| Command | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| `pipeline.py` | done | | refused: usage, a missing file, the sheet, a corrupt history line, a personal value in the request | refused: the model's answer or the API cannot be used; nothing written | outputs written, the webhook failed |
| `approval.py` | done, an export with nothing approved included | | refused; nothing written | | |
| `report.py` | written | | refused: not a run folder, decisions from another run, a changed draft | | |
| `demo.py` | every case ended as expected | a case did not | refused: usage | | |
| `make_sheet.py` | written | | refused: an existing file, a count out of range | | |
| `make_examples.py` | written | the demo case did not end as expected | | | |
| `run_tests.py` | every test passed | a test failed | | | |
