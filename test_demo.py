"""Tests of the failure gallery (demo.py and gallery/), the examples and the list of tests that make_examples.py
writes, and the output the README quotes.
Run: python -I test_demo.py  (every test file: python -I run_tests.py)"""
import csv
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import demo  # noqa: E402
import make_examples  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import run_tests  # noqa: E402
from testsupport import RECORDED, SHEET, TempTest, call_main  # noqa: E402

GALLERY = HERE / "gallery"
EXAMPLES = HERE / "examples"
CASE_NAMES = ["happy-path", "bad-sheet", "wrong-total", "invented-customer", "leak-guard", "second-run", "no-approval",
              "foreign-decisions", "api-error", "webhook-down"]


def sheet_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class DemoTest(TempTest):
    def test_the_list_names_the_ten_cases(self):
        code, printed, _ = call_main(demo, ["--list"])
        self.assertEqual(code, 0)
        self.assertEqual([line.split(":")[0] for line in printed.splitlines()], CASE_NAMES)

    def test_every_case_ends_as_expected(self):
        code, printed, _ = call_main(demo, ["--out", str(self.base), "--as-of", make_examples.AS_OF])
        lines = printed.splitlines()
        self.assertEqual(code, 0, printed)
        self.assertEqual(len(lines), 22)  # ten detailed lines, a blank line, the closing table, the total
        detailed, blank, table, total = lines[:10], lines[10], lines[11:21], lines[21]
        self.assertEqual([line.split(" | ")[0] for line in detailed], CASE_NAMES)
        self.assertTrue(all(line.endswith(" | PASS") for line in detailed), printed)
        self.assertEqual(blank, "")
        self.assertEqual([line.split()[0] for line in table], CASE_NAMES)
        self.assertTrue(all(line.endswith("  PASS") and len(line) <= demo.COMPACT_WIDTH for line in table), table)
        self.assertEqual(len({line.rindex("PASS") for line in table}), 1, "the verdicts line up")
        self.assertEqual(total, f"10 of 10 case(s) ended as expected | written under "
                                f"{(self.base / 'demo-001').as_posix()}")
        self.assertTrue((self.base / "demo-001" / "happy-path" / "report.html").is_file())

    def test_the_closing_table_cuts_a_long_text_but_never_the_verdict(self):
        rows = [("short-case", "a few words", True), ("long-case", "word " * 40, False)]
        lines = demo.closing_table(rows)
        self.assertTrue(all(len(line) <= demo.COMPACT_WIDTH for line in lines))
        self.assertTrue(lines[0].startswith("short-case  a few words ") and lines[0].endswith("  PASS"))
        self.assertTrue(lines[1].endswith("...  FAIL"), lines[1])

    def test_one_case_runs_alone_in_a_new_folder_each_time(self):
        for number in (1, 2):
            code, printed, _ = call_main(demo, ["--case", "bad-sheet", "--out", str(self.base)])
            self.assertEqual(code, 0)
            self.assertTrue(printed.startswith("bad-sheet | expected: refused before anything is sent, exit 2, "
                                               "nothing written | happened: exit 2; nothing written; REFUSED: row 2 "
                                               "of the sheet: amount_due '4,200' "), printed)
            self.assertEqual([p.name for p in (self.base / f"demo-{number:03d}").iterdir()], ["bad-sheet"])

    def test_a_case_that_does_not_end_as_expected_says_fail(self):
        fake = self.base / "gallery"
        shutil.copytree(GALLERY, fake)
        shutil.copyfile(RECORDED, fake / "wrong-total.json")  # nothing is wrong in it, so nothing is rejected
        with mock.patch.object(demo, "GALLERY", fake):
            code, printed, _ = call_main(demo, ["--case", "wrong-total", "--out", str(self.base / "out")])
        self.assertEqual(code, 1)
        lines = printed.splitlines()
        self.assertIn("| happened: exit 0; rejected no new rejection;", lines[0])
        self.assertTrue(lines[0].endswith(" | FAIL"))
        self.assertTrue(lines[2].startswith("wrong-total  exit 0, nothing new set aside,"))
        self.assertTrue(lines[2].endswith("FAIL"))
        self.assertTrue(lines[3].startswith("0 of 1 case(s) ended as expected"))

    def test_usage_is_refused(self):
        for argv, message in [([], "--out is required, except with --list"),
                              (["--case", "no-such-case", "--out", str(self.base)], "invalid choice"),
                              (["--out", str(self.base), "--at", "09:00"], "is not a UTC timestamp")]:
            code, _, err = call_main(demo, argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err)

    def test_the_api_case_puts_the_environment_back(self):
        for before in ("a value that was there before", None):
            with mock.patch.dict(os.environ, clear=False):
                os.environ.pop(pipeline.API_KEY_ENV, None)
                if before is not None:
                    os.environ[pipeline.API_KEY_ENV] = before
                code, printed, _ = call_main(demo, ["--case", "api-error", "--out", str(self.base)])
                self.assertEqual(code, 0, printed)
                self.assertEqual(os.environ.get(pipeline.API_KEY_ENV), before)


class GalleryTest(unittest.TestCase):
    def test_the_gallery_files_are_what_make_examples_writes(self):
        for name, data in make_examples.gallery_files().items():
            self.assertEqual((GALLERY / name).read_bytes(), data, f"gallery/{name} is out of date")

    def test_each_wrong_answer_is_the_recorded_answer_with_one_change(self):
        recorded = pipeline.parse_answer(RECORDED.read_text(encoding="utf-8"))
        wrong = pipeline.parse_answer((GALLERY / "wrong-total.json").read_text(encoding="utf-8"))
        changed = [(a, b) for a, b in zip(recorded["call_list"], wrong["call_list"]) if a != b]
        self.assertEqual(len(changed), 1)
        before, after = changed[0]
        self.assertEqual((before["priority"], after["amount"] - before["amount"]), (2, 100))
        self.assertEqual(dict(after, amount=before["amount"]), before)
        self.assertEqual(dict(wrong, call_list=recorded["call_list"]), recorded)
        invented = pipeline.parse_answer((GALLERY / "invented-customer.json").read_text(encoding="utf-8"))
        self.assertEqual(dict(invented, call_list=invented["call_list"][:-1]), recorded)
        added = invented["call_list"][-1]
        self.assertEqual(added["customer"], make_examples.invented_stand_in(sheet_rows(SHEET)))
        self.assertNotIn(added["customer"], pipeline.prepare(SHEET, HERE / "prompt.txt")["key"])

    def test_each_broken_sheet_is_the_shipped_sheet_with_one_change(self):
        shipped = sheet_rows(SHEET)
        for name, column, value in [("bad-sheet.csv", "amount_due", "4,200"),
                                    ("leak-sheet.csv", "notes", "Ask {}\n{} to call back")]:
            rows = sheet_rows(GALLERY / name)
            self.assertEqual(rows[1:], shipped[1:], name)
            self.assertEqual(dict(rows[0], **{column: shipped[0][column]}), shipped[0], name)
            expected = value.format(*shipped[0]["name"].split(" ", 1)) if "{}" in value else value
            self.assertEqual(rows[0][column], expected, name)

    def test_the_gallery_readme_counts_the_cases_that_read_it(self):
        reading = [name for name, _, _, case in demo.CASES if "GALLERY" in inspect.getsource(case)]
        self.assertEqual(reading, ["bad-sheet", "wrong-total", "invented-customer", "leak-guard", "foreign-decisions"])
        readme = " ".join((GALLERY / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("Five of them read the files in this folder: " + ", ".join(f"`{n}`" for n in reading[:-1])
                      + f" and `{reading[-1]}`.", readme)

    def test_the_gallery_readme_names_every_file_and_case(self):
        readme = (GALLERY / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Every file here is constructed, never recorded.**", readme)
        for name in make_examples.gallery_files():
            self.assertIn(f"| `{name}` |", readme)
        for name in CASE_NAMES:
            self.assertIn(f"| `{name}` |", readme)
            self.assertIn(f"`python -I demo.py --case {name} --out out-demo`", readme)
        self.assertIn(f"for `{make_examples.invented_stand_in(sheet_rows(SHEET))}`", readme)


class ExamplesTest(unittest.TestCase):
    def test_the_examples_are_what_make_examples_writes(self):
        files = make_examples.example_files()
        self.assertIsNotNone(files)
        on_disk = {p.relative_to(EXAMPLES).as_posix(): p.read_bytes() for p in EXAMPLES.rglob("*") if p.is_file()}
        self.assertEqual(sorted(on_disk), sorted(files))
        for name, data in files.items():
            self.assertEqual(on_disk[name], data, f"examples/{name} is out of date: run python -I make_examples.py")

    def test_the_example_export_holds_only_what_was_approved(self):
        decisions = json.loads((EXAMPLES / "decisions.json").read_text(encoding="utf-8"))["decisions"]
        approved = sorted(e["item"] for e in decisions if e["decision"] == "approved")
        self.assertEqual(sorted(p.name for p in (EXAMPLES / "approved" / "reminders").iterdir()),
                         [f"reminder-{i:02d}.txt" for i in approved])
        with (EXAMPLES / "approved" / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual([int(row["priority"]) for row in csv.DictReader(f)], approved)
        self.assertEqual(len((EXAMPLES / "history.jsonl").read_text(encoding="utf-8").splitlines()), len(approved))
        run_report = json.loads((EXAMPLES / "run-report.json").read_text(encoding="utf-8"))
        for name in ("call-list.csv", "reminders/reminder-01.txt"):
            data = (EXAMPLES / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), run_report["drafts_sha256"][name], name)

    def test_the_demo_picture_is_drawn_from_a_real_run(self):
        picture = (HERE / "docs" / "img" / "demo.svg").read_bytes()
        self.assertEqual(picture, make_examples.demo_picture(),
                         "docs/img/demo.svg is out of date: run python -I make_examples.py")
        text = picture.decode("utf-8")
        self.assertIn(f">$ {make_examples.PICTURE_COMMAND}</text>", text)
        for name in CASE_NAMES:
            self.assertIn(f">{name} ", text)
        self.assertIn(">10 of 10 case(s) ended as expected | written under out-demo/demo-001</text>", text)
        self.assertNotIn("<script", text)

    def test_the_list_of_tests_is_up_to_date(self):
        self.assertEqual((HERE / "docs" / "TESTS.md").read_text(encoding="utf-8"), run_tests.markdown(),
                         "docs/TESTS.md is out of date: run python -I make_examples.py")

    def test_the_examples_hold_no_path_of_this_computer(self):
        for path in list(EXAMPLES.rglob("*")) + list(GALLERY.rglob("*")) + [HERE / "docs" / "img" / "demo.svg"]:
            if path.is_file():
                text = path.read_bytes().decode("utf-8")
                for marker in (str(HERE), HERE.as_posix(), "C:\\", "/home/", "/tmp/", "\\Users\\"):
                    self.assertNotIn(marker, text, path)


class ReadmeTest(TempTest):
    """The README quotes real output; these tests hold it to that."""

    def setUp(self):
        super().setUp()
        self.readme = (HERE / "README.md").read_text(encoding="utf-8")
        self.lines = self.readme.splitlines()

    def section(self, heading):
        return self.readme.split(f"\n## {heading}\n")[1].split("\n## ")[0]

    def test_the_first_screen_quotes_a_real_reminder_and_the_call_list(self):
        screen = self.section("What you see")
        reminder = (EXAMPLES / "reminders" / "reminder-01.txt").read_text(encoding="utf-8").rstrip("\n")
        self.assertIn("\n> " + reminder + "\n", screen)
        with (EXAMPLES / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))[:4]
        table = ["| Priority | Customer | Total | Days late | The model's reason |", "|---|---|---|---|---|"]
        table += [f"| {r['priority']} | {r['customer']} | {r['amount']} | {r['days_late']} | {r['reason']} |"
                  for r in rows]
        self.assertIn("\n".join(table), screen)
        self.assertIn("[`examples/report.html`](examples/report.html)", screen)
        self.assertIn("a page to open in a browser after cloning the repository", screen)
        self.assertNotIn("![", screen)  # no picture on the first screen until one exists from a real render

    def test_every_image_a_markdown_file_embeds_exists(self):
        files = [p for p in HERE.rglob("*.md")
                 if not any(part.startswith((".", "out-")) for part in p.relative_to(HERE).parts)]
        self.assertIn(HERE / "README.md", files)
        for path in files:
            for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", path.read_text(encoding="utf-8")):
                if not target.startswith(("http://", "https://")):
                    self.assertTrue((path.parent / target).is_file(), f"{path.name} embeds {target}")

    def test_the_terms_are_explained_where_they_first_appear(self):
        body = self.readme.split("\n", 1)[1].lower()
        for term, explanation in [("stand-in", "the placeholder the model sees instead of a personal value"),
                                  ("ageing", "how long the money has been owed"),
                                  ("recorded answer", "one model answer saved to a file and replayed by every run")]:
            first = body.index(term)
            self.assertIn(explanation, body[first:first + 160], term)

    def test_the_picture_of_the_demo_is_with_what_can_go_wrong_and_usage_conditions_come_last(self):
        self.assertIn("](docs/img/demo.svg)", self.section("What can go wrong, and what happens"))
        headings = [line[3:] for line in self.lines if line.startswith("## ")]
        self.assertEqual(headings[-3:], ["Usage conditions", "Licence", "Author"])
        conditions = self.section("Usage conditions")
        for words in ("a demonstration on invented data", "Do not point it at real customer data", "no warranty",
                      "MIT"):
            self.assertIn(words, conditions)

    def test_what_leaves_the_computer_is_said_where_sending_is(self):
        self.assertIn("nothing is ever sent to a customer, and only when asked does anything leave the computer (the "
                      "request to the model on the live path, and a run summary to a webhook)", self.lines[2])
        self.assertNotRegex(self.readme.lower(), r"nothing is (ever )?sent\.")
        self.assertIn("Two things leave the computer, and only when asked: the request to the model on the live path",
                      self.readme)

    def test_the_readme_states_the_recordings_result_and_its_origin_as_a_statement(self):
        prep = pipeline.prepare(SHEET, HERE / "prompt.txt")
        accepted, rejected, missing, _, rejected_notes = pipeline.validate(
            pipeline.parse_answer(RECORDED.read_text(encoding="utf-8")), prep["expected"], prep["key"])
        self.assertEqual((len(accepted), rejected, missing, len(rejected_notes)), (22, [], [], 1))
        text = " ".join(self.readme.split())
        self.assertIn(f"\"{rejected_notes[0]['note']}\"", text)
        self.assertIn(f"({rejected_notes[0]['reason']})", text)
        self.assertIn("The checks accept all 22 owing customers of it and set aside one note for a person", text)
        real = self.section("What is real here")
        for words in ("By the author's statement", "runs under its own system prompt", "cannot prove the origin",
                      "proves only that the file has not changed since it was saved"):
            self.assertIn(words, real)
        self.assertIn("with one exception: that the recorded answer is a model's answer rests on the author's "
                      "statement", self.section("How this was built"))

    def test_the_figure_check_is_stated_exactly(self):
        limits = " ".join(self.section("Limits, stated plainly").split())
        for words in ("Every number written in digits must be one of that customer's own figures",
                      "It does not know which number means what, so two right numbers swapped",
                      "a loose reference to a stand-in", "a numeral other than 0 to 9",
                      "a per cent sign or a web address"):
            self.assertIn(words, limits)

    def test_the_quoted_first_lines_are_what_the_commands_print(self):
        self.assertEqual(demo.CASES[0][0], "happy-path")  # so the whole demo's first line is the happy path's
        for case in ("happy-path", "bad-sheet"):
            code, printed, _ = call_main(demo, ["--case", case, "--out", str(self.base / "out-demo")])
            self.assertEqual(code, 0)
            self.assertIn(printed.splitlines()[0], self.lines)
        run = self.base / "out-demo" / "demo-001" / "happy-path" / "run"
        code, printed, _ = call_main(report, [str(run), "--out", str(self.base / "out-demo" / "report.html")])
        self.assertEqual(code, 0)
        self.assertIn(printed.strip().replace(self.base.as_posix() + "/", ""), self.lines)
        for command in ("python -I demo.py --out out-demo", "python -I demo.py --case bad-sheet --out out-demo",
                        "python -I report.py out-demo/demo-001/happy-path/run --out out-demo/report.html"):
            self.assertIn(command, self.lines)

    def test_the_decision_commands_print_what_the_readme_quotes(self):
        stamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")  # the time of a decision is the time it was taken
        run, history, reason = str(self.base / "out-run"), str(self.base / "out-history.jsonl"), \
            "Settle the instalment request first"
        steps = [("python -I pipeline.py --out out-run", pipeline, ["--out", run]),
                 ("python -I approval.py list out-run", approval, ["list", run]),
                 ("python -I approval.py show out-run --item 1", approval, ["show", run, "--item", "1"]),
                 ('python -I approval.py approve out-run --item 1 --by "Your Name"', approval,
                  ["approve", run, "--item", "1", "--by", "Your Name"]),
                 (f'python -I approval.py reject out-run --item 2 --by "Your Name" --reason "{reason}"', approval,
                  ["reject", run, "--item", "2", "--by", "Your Name", "--reason", reason]),
                 ("python -I approval.py export out-run --history out-history.jsonl", approval,
                  ["export", run, "--history", history])]
        for command, module, argv in steps:
            code, printed, err = call_main(module, argv)
            self.assertEqual(code, 0, err)
            quoted = self.lines[self.lines.index("$ " + command) + 1]
            self.assertEqual(stamp.sub("<time>", quoted), stamp.sub("<time>", printed.splitlines()[0]), command)

    def test_the_opening_is_the_title_then_two_sentences_and_the_badges_start_try_it(self):
        self.assertEqual(self.lines[0], "# AI Collections Follow-up")
        opening = self.lines[2]
        self.assertEqual(self.lines[4], "## What you see")
        self.assertEqual(opening.count(". "), 1)  # two sentences, neither in the first person
        self.assertNotRegex(opening, r"\b(I|me|my|we|our)\b")
        self.assertIn("demonstration on invented data", opening)
        try_it = self.section("Try it in two minutes").splitlines()
        badges = re.findall(r"!\[(\w+)\]", try_it[1])
        self.assertEqual(badges, ["tests", "python", "dependencies", "data", "license"])
        self.assertEqual(self.readme.count("actions/workflows/tests.yml/badge.svg"), 1)

    def test_the_stated_ci_versions_are_the_workflows(self):
        workflow = (HERE / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn('        os: [ubuntu-latest, windows-latest]\n        python-version: ["3.11", "3.13"]\n',
                      workflow)
        self.assertIn("on Linux and Windows with Python 3.11 and 3.13.", self.readme)
        self.assertIn("![python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)", self.readme)

    def test_what_is_real_has_no_live_row_and_every_case_has_its_command(self):
        table = self.readme.split("## What is real here")[1].split("\n## ")[0]
        kinds = [row.split(" | ")[1] for row in table.splitlines() if row.startswith("| ") and " | " in row]
        self.assertEqual(kinds[1:], ["Recorded", "Mock", "Mock", "Local"])
        self.assertNotIn("Live", kinds)
        for name in CASE_NAMES:
            self.assertIn(f"| `{name}` |", self.readme)
            self.assertIn(f"`python -I demo.py --case {name} --out out-demo`", self.readme)

    def test_no_text_describes_an_earlier_recording(self):
        for stale in ("placeholder constructed", "constructed placeholder", "no real model answer", "at this commit"):
            self.assertNotIn(stale, self.readme.lower())
        for path in (HERE / "README.md", HERE / "docs" / "DESIGN.md", EXAMPLES / "README.md", GALLERY / "README.md"):
            text = " ".join(path.read_text(encoding="utf-8").split())  # line breaks in the text do not count
            self.assertNotIn("008, 019 and 020", text, path.name)
            self.assertNotIn("not in the sheet: 19, 20", text, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
