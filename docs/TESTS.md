# The tests

Written by `python -I make_examples.py` from the names of the tests; do not edit it by hand. Run every test with `python -I run_tests.py`; each file also runs on its own.

136 tests in 7 files.

## test_approval.py

Tests of the approval step: decisions recorded, appended and refused, and the export of only what was approved.

### ApprovalTest

- a customer already exported inside the window is not exported again
- a decision needs a known item a name a reason and a real timestamp
- a failure after the history was appended puts it back
- a folder that is not a run and a broken decisions file are refused
- a history that cannot be written leaves nothing and the export can be tried again
- an export appends after a last line without a line end
- an export with nothing approved exports nothing and is not an error
- approvals and rejections are recorded with who when and why
- decisions are appended and the last one counts
- every draft starts pending
- export refuses a corrupt history before writing anything
- export refuses a draft changed after it was written
- export refuses a second export and a decision after one
- export refuses decisions that name another run
- export writes only the approved drafts and the history
- the next run leaves out what was exported
- without at the time is now in utc

## test_checks.py

Tests of the checks added after an independent review: loose references to stand-ins, what a reminder may not hold, the sheet check's limits, the leak guard's whole words, and the amounts kept exact after the scrub.

### FormulaCellTest

A cell a spreadsheet could run as a formula is written with a leading apostrophe in every CSV file.

- a by that starts like a formula is refused and the export adds no second apostrophe
- a name and a reason that start like a formula are written as text
- the pseudonymised and the generated sheets are guarded too

### SheetCheckTest

- a header that differs only in letter case is refused with the expected name
- an amount with more than 15 digits before the point is refused
- the amounts are restored after the scrub
- the leak guard matches whole words as the scrubber does

### TextCheckTest

Every checked text: the note for a person, the reason and the reminder.

- a leading zero inside a decimal a thousands group or a stand in is not loose
- a loose reference is refused in a reason and in a reminder
- a loose reference to stand ins is set aside in a note
- a numeral other than 0 to 9 is refused everywhere
- a reminder may not hold a per cent sign or a web address

## test_demo.py

Tests of the failure gallery (demo.py and gallery/), the examples and the list of tests that make_examples.py writes, and the output the README quotes.

### DemoTest

- a case that does not end as expected says fail
- every case ends as expected
- one case runs alone in a new folder each time
- the api case puts the environment back
- the closing table cuts a long text but never the verdict
- the list names the ten cases
- usage is refused

### ExamplesTest

- the demo picture is drawn from a real run
- the example export holds only what was approved
- the examples are what make examples writes
- the examples hold no path of this computer
- the list of tests is up to date

### GalleryTest

- each broken sheet is the shipped sheet with one change
- each wrong answer is the recorded answer with one change
- the gallery files are what make examples writes
- the gallery readme names every file and case

### ReadmeTest

The README quotes real output; these tests hold it to that.

- no text describes an earlier recording
- the call list sample is the first five lines of the example
- the decision commands print what the readme quotes
- the opening is the title then the badges then two sentences
- the quoted first lines are what the commands print
- the stated ci versions are the workflows
- what is real has no live row and every case has its command

## test_history.py

Tests of the run history: what an earlier export leaves out of the next request, and what refuses a run.

### HistoryTest

- a changed total is drafted again and marked
- a corrupt history line refuses the run with its line number
- a customer exported inside the window with the same total is left out
- a missing history file is an empty history
- a replayed answer saved for another request is named in one line
- an answer for a customer left out is listed apart and not counted as refused
- bad dates and windows are refused by the command line
- print request leaves the drafted customer out
- the history key is a hash and holds no name
- the latest line inside the window counts
- the window has a clear edge
- two spellings of a name are two customers in the history too
- when every owing customer was drafted the model is not called
- without history the run is as before

## test_pipeline.py

Tests of the pipeline and the pseudonymiser, on the six-row sheet in fixtures/ and on small sheets written by the tests themselves. No network outside this computer: the API and the webhook are local mock servers.

### GuardTest

- a correct answer is accepted whole
- a customer left out is listed as missing
- a fenced answer is read and anything else is refused
- a figure added to a reminder is rejected with what it is
- a reason may state only the customers own figures and stand ins
- a reminder may name only its own customer
- a reminder must be a text and state the total
- a wrong total is rejected
- an invented customer and a paid one are rejected
- an item that is not an object is rejected
- figures of the wrong kind are rejected
- more than 999 customers keep exact stand ins
- nan and infinity refuse the whole answer
- notes for a human are checked one by one
- the same customer twice is rejected
- wrong days late and a bad priority are rejected

### MoneyTest

- a rejection quotes amounts exactly
- amounts are exact decimals from the sheet to the call list
- amounts are written in plain notation

### RequestTest

- only the four needed columns are sent
- print request prints the exact request and needs no out
- the model request carries no personal value
- the model table is written by the csv module
- the recorded answer meta file matches the request

### RunTest

- a failed webhook keeps the outputs and exits with 4
- an existing folder is never overwritten
- an unusable answer writes nothing
- api failures are refused and write nothing
- end to end restores names only locally
- no key means a clear refusal and nothing sent
- the api path against a local mock
- the page for a person names who was rejected or left out
- the recorded model answer runs through the guard
- the webhook gets stand ins only and remote is refused

### ScrubTest

- a value written differently reaches the model as the readme says
- known values are replaced as whole words in any letter case
- the command line writes the clean sheet and the key and refuses to overwrite
- the longest value is replaced first
- the three patterns catch values outside their columns

### SheetTest

The sheet is checked before anything is sent; a sheet that fails never reaches the model.

- a header spelled differently is refused before sending
- a missing column is refused
- bad cells are refused with the row and the column
- missing files are refused
- the leak guard refuses a personal value before sending

## test_report.py

Tests of the report page: its parts and their order, its figures from the run's own files, escaping, and refusals.

### ReportTest

- a folder that is not a run or decisions from another run are refused
- amounts are shown for a reader and never rounded
- an answer for a customer already drafted is listed apart
- every figure comes from the run files
- refusals the history and a changed amount are shown
- text from the model or the sheet is shown as text
- the label says live only for the default api address
- the labels are earned from the files
- the page has its nine parts in order
- the page is self contained
- the page shows the decisions when it is written again

## test_sheet.py

Tests of the generated sheet (make_sheet.py), the ageing buckets, and the shipped recorded answer.

### AgeingTest

- the bucket is in the call list and the run report
- the bucket is never sent to the model
- the buckets at their edges
- the delay and the bucket come from the rows that still owe money

### RecordedAnswerTest

- the answer file has not changed since it was saved
- the constructed answer passes every check
- the recorded answer was saved for the request built today
- the shipped recorded answer runs through the guard

### SheetGeneratorTest

- an existing file and a count out of range are refused
- the largest count still gives every customer a name of their own
- the same seed gives the same sheet and another seed another
- the shipped request carries no personal value
- the shipped sheet has what the specification asks
- the shipped sheet is the generators output for the committed seed
- the shipped sheets hashes are the files
