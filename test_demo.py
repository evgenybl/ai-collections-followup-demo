"""Tests of the failure gallery (demo.py and gallery/), the examples and the list of tests that make_examples.py
writes, and the output the README quotes.
Run: python -I test_demo.py  (every test file: python -I run_tests.py)"""
import csv
import hashlib
import json
import os
import pathlib
import shutil
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import demo  # noqa: E402
import make_examples  # noqa: E402
import pipeline  # noqa: E402
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
        self.assertEqual([line.split(" | ")[0] for line in lines[:-1]], CASE_NAMES)
        self.assertTrue(all(line.endswith(" | PASS") for line in lines[:-1]), printed)
        self.assertEqual(lines[-1], f"10 of 10 case(s) ended as expected | written under "
                                    f"{(self.base / 'demo-001').as_posix()}")
        self.assertTrue((self.base / "demo-001" / "happy-path" / "report.html").is_file())

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
        self.assertIn("| happened: exit 0; rejected no new rejection;", printed)
        self.assertTrue(printed.splitlines()[0].endswith(" | FAIL"))
        self.assertTrue(printed.splitlines()[1].startswith("0 of 1 case(s) ended as expected"))

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
        report = json.loads((EXAMPLES / "run-report.json").read_text(encoding="utf-8"))
        for name in ("call-list.csv", "reminders/reminder-01.txt"):
            data = (EXAMPLES / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), report["drafts_sha256"][name], name)

    def test_the_list_of_tests_is_up_to_date(self):
        self.assertEqual((HERE / "docs" / "TESTS.md").read_text(encoding="utf-8"), run_tests.markdown(),
                         "docs/TESTS.md is out of date: run python -I make_examples.py")

    def test_the_examples_hold_no_path_of_this_computer(self):
        for path in list(EXAMPLES.rglob("*")) + list(GALLERY.rglob("*")):
            if path.is_file():
                text = path.read_bytes().decode("utf-8")
                for marker in (str(HERE), HERE.as_posix(), "C:\\", "/home/", "/tmp/", "\\Users\\"):
                    self.assertNotIn(marker, text, path)


if __name__ == "__main__":
    unittest.main(verbosity=1)
