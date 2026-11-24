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
| `spreadsheet.py` | What every CSV file of the project does to a cell a spreadsheet could run as a formula |
| `demo.py` | The failure gallery: ten named cases through the real command lines |
| `make_sheet.py` | The synthetic sheet, generated from a seed |
| `make_examples.py` | Writes `gallery/`, `examples/`, `docs/img/demo.svg` and `docs/TESTS.md` again from their sources |
| `run_tests.py` | Runs every `test_*.py` file in one command |
| `tools/term_svg.py` | Draws a command's real output as an SVG picture of a terminal |
| `prompt.txt`, `customers.synthetic.csv`, `recorded-answer.json` | The fixed prompt, the shipped sheet, and the answer the demo replays, with its meta file |
| `fixtures/` | The first version's six-row sheet, its prompt, and the answer recorded for them, which the original tests still use |

## What a run leaves behind

```
<run folder>/                written by pipeline.py; it refuses a folder that exists
  call-list.csv              the accepted items in priority order, real names, with the ageing bucket
  reminders/reminder-NN.txt  one draft per accepted item, NN being its priority
  for-a-person.txt           notes, refusals, missing customers, and a key of the customers, real names
  for-a-person.json          the same, in a form report.py reads
  run-report.json            stand-ins only: counts, refusals, ageing, the sha256 of every draft as written, and the
                             facts the report's labels are earned from
  decisions.json             written by approval.py approve and reject
  approved/                  written once by approval.py export: the approved call-list lines and reminders
<history file>               JSON lines, appended by approval.py export, read by the next pipeline.py run
```

## Decisions, and why

**The history is written at export, not when a draft is written.** A draft nobody approved was never used. If
drafting wrote the history, a run whose drafts were rejected or never looked at would keep those customers out of the
next run for the whole window, and they would silently not be followed up. Writing at export ties "already drafted"
to "a person approved it and it left the run folder". Two runs drafted before any export draft the same customers
twice, in two folders; the second export then finds the first one's history lines and does not export those customers
again.

**Only the same total keeps a customer out.** If the total changed, the earlier reminder states a wrong figure now, so
the customer is drafted again and marked "amount changed since the last draft". Of several history lines for a
customer inside the window, the latest counts, and of two of the same date the later one in the file. The window has
one definition, in the code's help and here: a draft exported fewer than N days before the as-of date, so ages 0 to
N-1 (N is `--window-days`, 7 by default). A line dated after the run's date is refused rather than ignored or read as
old: either the date of the run or the history is wrong, and the tool does not guess which. An item a replayed answer
holds for a customer the history left out is listed as answered although already drafted, not counted as refused: it
answers another request.

**The export is all or nothing.** `approved/` is built in a temporary folder, the history is appended, and only then is
the folder moved into place. If any step fails, the temporary folder is removed and the history put back as it was, so
the export can be tried again; it never leaves an `approved/` folder whose history line was not written.

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

**The figure check knows figures, not meanings.** Every number written in digits must be one of that customer's own
figures (in a reminder only the total and the days late; in a note for a person any figure of the sheet). The check
does not know which number means what, so a reminder with the total and the days late swapped passes. It also refuses
what it could only pass by chance: a number with a leading zero outside a stand-in written in full, such as 005 in
"Customers 005, 007 and 016" (a loose reference to a stand-in), and a numeral other than 0 to 9. A reminder may not
hold a per cent sign or a web address. The recorded answer shows why the loose reference matters: its note
"Customers 005, 007 and 016 promised to pay by the end of the month; check whether the promise is kept." passed the
figure check only because 5, 7 and 16 happen to be figures in the sheet. It is now set aside (the note holds a loose
reference to a stand-in: 005, 007, 016), and the page for a person ends with a key of the customers, so that a person
can read it.

**The report's labels are earned from recorded facts.** The run report records the replayed file's sha256, whether it
and the request equal the shipped recording's meta file, the sheet's sha256, and whether that sheet is one this
repository ships. The page only reads them: "Recorded" only for the shipped recording on its own request, "Recorded
answer, replayed for another request" when the history changed the request, "Replayed file, origin not verified" for
any other file; "Synthetic data" only for a shipped sheet, otherwise "Sheet supplied by the user"; "Live" only for
the API's default address.

**A recording's origin is a statement, not a proof.** That `recorded-answer.json` is a model's answer is the author's
statement: it was obtained by giving a Claude Code subagent the request text as its whole prompt. Such a subagent runs
under its own system prompt, so it is not the same call `--backend anthropic` makes. The sha256 in the meta file proves
only that the file has not changed since it was saved and that it answers the request the pipeline builds today.

**The gallery is constructed.** A model cannot be made to fail on cue, and a real failure would not come back the same
way twice. Each wrong answer is the recorded answer with one documented change, written again by `make_examples.py`
whenever the recording changes, and each case is judged against what the recorded answer itself gives. So the gallery
shows what the checks do with a given mistake; it says nothing about how often a model makes it.

**Ageing is derived when the sheet is read, never stored and never sent to the model.** It comes from the largest delay among the
rows that still owe money, which is also the delay the answer's days_late must equal: a paid row's older delay is a
figure of the customer's, not their delay. The model is not told the bucket, so the request, and the sha256 a recording
is tied to, do not depend on it.

**The history key is a hash, and a hash of a name is not anonymous.** It is a sha256 over a fixed prefix, the name as
the pipeline tells customers apart by it (the name column as written, without spaces at its ends) and the identity
numbers as written. Nothing else is normalised, so two spellings of a name are two customers here as everywhere. The
history file holds no name, but anyone who has the sheet can compute the same key, and a name can be guessed and
tested against it.

**A cell that could run as a formula is written as text.** In every CSV file the project writes, a cell that starts with
=, +, -, @, a tab or a carriage return gets a leading apostrophe (`spreadsheet.py`), and the page for a person says how
many cells were changed. The table in the request to the model is not changed: it is text for the model, and changing
it would change the request.

**The webhook reaches the address it was given, and nothing else.** Its "this computer only" rule is checked on that
address, so the request goes through no proxy from the environment and follows no redirect; a redirect is a failure.

**The sheet is generated from a seed with sha256, not with the random module.** Python promises to keep only
`random()` itself the same across versions, not `choice()`, `shuffle()` or `randrange()`. CI generates the sheet on
two Python versions and two systems and compares it byte for byte with the shipped one.

**Files are written with LF line ends on every system.** A draft's sha256 then does not depend on the system that wrote
it, so the examples can be compared byte for byte wherever they are written again. The call lists keep the csv
module's CRLF line ends, as they always had, and `.gitattributes` keeps those bytes in `examples/` as they are.

**The original tests keep their data.** The shipped sheet is now generated and the shipped prompt has changed, so the
six-row sheet, the prompt of that time and the recording made for them are in `fixtures/`, and the original tests read
them there with their assertions unchanged.

**Nothing is sent to a customer.** Nothing in the repository sends anything to a customer, and `sent_to_customers` is 0
in every run report; a test keeps it there. Two things leave the computer, and only when asked: the request to the
model on the live path (`--backend anthropic`), and the run summary, with stand-ins only, to a webhook (`--webhook`).

## Exit codes

| Command | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| `pipeline.py` | done | | refused: usage, a missing file, the sheet, a corrupt history line, a personal value in the request | refused: the model's answer or the API cannot be used; nothing written | outputs written, the webhook failed |
| `approval.py` | done, an export with nothing approved included | | refused; nothing written | | |
| `report.py` | written | | refused: not a run folder, decisions from another run, a changed draft | | |
| `demo.py` | every case ended as expected | a case did not | refused: usage | | |
| `make_sheet.py` | written | | refused: an existing file, a count out of range | | |
| `make_examples.py` | written | a demo run did not end as expected | | | |
| `run_tests.py` | every test passed | a test failed | | | |
