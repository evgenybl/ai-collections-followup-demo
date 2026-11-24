"""Tests of the run history: what an earlier export leaves out of the next request, and what refuses a run.
Run: python -I test_history.py  (every test file: python -I run_tests.py)"""
import contextlib
import csv
import datetime
import io
import json
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline  # noqa: E402
from testsupport import PROMPT, SHEET, SpyBackend, TempTest, call_main, constructed_answer  # noqa: E402

AS_OF = datetime.date(2026, 11, 24)


def days_before(n):
    return (AS_OF - datetime.timedelta(days=n)).isoformat()


class HistoryTest(TempTest):
    def setUp(self):
        super().setUp()
        self.prep = pipeline.prepare(SHEET, PROMPT)
        self.owing = sorted(who for who, c in self.prep["expected"].items() if c["total"] > 0)
        self.who = self.owing[0]
        self.real_name = self.prep["key"][self.who]

    def line(self, who, exported_on, total=None):
        total = pipeline.money(self.prep["everyone"][who]["total"]) if total is None else total
        return json.dumps({"customer_key": self.prep["customer_keys"][who], "exported_on": exported_on,
                           "total": total, "request_sha256": "ab" * 32})

    def history(self, *lines):
        return self.write("history.jsonl", "".join(line + "\n" for line in lines))

    def answer_for(self, history_path, window_days=pipeline.DEFAULT_WINDOW_DAYS):
        """A constructed answer to the request this history leaves: every owing customer still in it."""
        records = pipeline.read_history(history_path, AS_OF)
        return self.answer_file(constructed_answer(pipeline.prepare(SHEET, PROMPT, records, AS_OF, window_days)))

    def run_main(self, answer, *extra, out="run"):
        return call_main(pipeline, ["--sheet", str(SHEET), "--recorded", str(answer), "--out", str(self.base / out),
                                    "--as-of", AS_OF.isoformat(), *extra])

    def report(self, out="run"):
        return json.loads((self.base / out / "run-report.json").read_text(encoding="utf-8"))

    def test_without_history_the_run_is_as_before(self):
        code, printed, _ = self.run_main(self.answer_file(constructed_answer(self.prep)))
        self.assertEqual(code, 0)
        self.assertNotIn("history", printed)
        self.assertNotIn("model not called", printed)
        report = self.report()
        self.assertEqual((report["history_lines"], report["already_drafted"], report["model_called"]), (None, [], True))
        page = (self.base / "run" / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertNotIn("Already drafted", page)
        self.assertNotIn("Drafted again", page)

    def test_a_missing_history_file_is_an_empty_history(self):
        missing = self.base / "no-history-yet.jsonl"
        code, printed, _ = self.run_main(self.answer_file(constructed_answer(self.prep)), "--history", str(missing))
        self.assertEqual(code, 0)
        self.assertIn("| history lines 0 | already drafted 0 | amount changed 0", printed)
        self.assertEqual(pipeline.prepare(SHEET, PROMPT, pipeline.read_history(missing, AS_OF), AS_OF)["request"],
                         self.prep["request"])
        self.assertFalse(missing.exists(), "a run never writes the history; only an export does")

    def test_a_customer_exported_inside_the_window_with_the_same_total_is_left_out(self):
        history = self.history(self.line(self.who, days_before(3)))
        records = pipeline.read_history(history, AS_OF)
        request = pipeline.prepare(SHEET, PROMPT, records, AS_OF)["request"]
        self.assertIn(f"\n{self.who},", self.prep["request"])
        self.assertNotIn(f"\n{self.who},", request)
        code, printed, _ = self.run_main(self.answer_for(history), "--history", str(history))
        self.assertEqual(code, 0)
        self.assertIn("| history lines 1 | already drafted 1 | amount changed 0", printed)
        report = self.report()
        total = pipeline.money(self.prep["everyone"][self.who]["total"])
        self.assertEqual(report["already_drafted"], [{"customer": self.who, "drafted_on": days_before(3),
                                                      "total": total}])
        self.assertEqual(report["accepted"], len(self.owing) - 1)
        self.assertEqual(report["customers_owing"], len(self.owing))  # the sheet still owes it
        self.assertNotIn(self.who, [d["customer"] for d in report["drafts"]])
        page = (self.base / "run" / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertIn(f"(left out of the request):\n- {self.real_name}: drafted on {days_before(3)}, total {total}\n",
                      page)
        call_list = (self.base / "run" / "call-list.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn(self.real_name, call_list)

    def test_a_changed_total_is_drafted_again_and_marked(self):
        history = self.history(self.line(self.who, days_before(2), total="1"))
        code, printed, _ = self.run_main(self.answer_for(history), "--history", str(history))
        self.assertEqual(code, 0)
        self.assertIn("| already drafted 0 | amount changed 1", printed)
        report = self.report()
        total = pipeline.money(self.prep["everyone"][self.who]["total"])
        self.assertEqual(report["amount_changed"], [{"customer": self.who, "total": total, "last_total": "1",
                                                     "last_drafted_on": days_before(2)}])
        self.assertEqual([d["amount_changed"] for d in report["drafts"] if d["customer"] == self.who], [True])
        self.assertEqual(report["accepted"], len(self.owing))
        page = (self.base / "run" / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertIn(f"Drafted again: the amount changed since the last draft:\n- {self.real_name}: {total} now, "
                      f"1 when drafted on {days_before(2)}\n", page)

    def test_the_window_has_a_clear_edge(self):
        window = pipeline.DEFAULT_WINDOW_DAYS
        for age, left_out in [(0, True), (window - 1, True), (window, False), (window + 30, False)]:
            records = pipeline.read_history(self.history(self.line(self.who, days_before(age))), AS_OF)
            drafted = pipeline.prepare(SHEET, PROMPT, records, AS_OF)["already_drafted"]
            self.assertEqual(self.who in drafted, left_out, age)
        records = pipeline.read_history(self.history(self.line(self.who, days_before(window + 30))), AS_OF)
        self.assertIn(self.who, pipeline.prepare(SHEET, PROMPT, records, AS_OF, window + 31)["already_drafted"])

    def test_the_latest_line_inside_the_window_counts(self):
        same, other = None, "1"
        for older, newer, left_out in [(same, other, False), (other, same, True)]:
            history = self.history(self.line(self.who, days_before(4), older),
                                   self.line(self.who, days_before(1), newer))
            prep = pipeline.prepare(SHEET, PROMPT, pipeline.read_history(history, AS_OF), AS_OF)
            self.assertEqual((self.who in prep["already_drafted"], self.who in prep["amount_changed"]),
                             (left_out, not left_out))

    def test_of_two_lines_of_the_same_date_the_later_in_the_file_counts(self):
        for first, second, left_out in [(None, "1", False), ("1", None, True)]:
            history = self.history(self.line(self.who, days_before(2), first), self.line(self.who, days_before(2),
                                                                                         second))
            prep = pipeline.prepare(SHEET, PROMPT, pipeline.read_history(history, AS_OF), AS_OF)
            self.assertEqual(self.who in prep["already_drafted"], left_out, (first, second))

    def test_when_every_owing_customer_was_drafted_the_model_is_not_called(self):
        history = self.history(*[self.line(who, days_before(1)) for who in self.owing])
        spy = SpyBackend("this text would be refused if the model were asked")
        report = pipeline.run(SHEET, PROMPT, spy, self.base / "spy", history=history, as_of=AS_OF)
        self.assertEqual((spy.calls, report["model_called"], report["accepted"]), (0, False, 0))
        self.assertEqual(len(report["already_drafted"]), len(self.owing))
        self.assertEqual((report["rejected"], report["missing_from_answer"], report["drafts"]), ([], [], []))
        with (self.base / "spy" / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.reader(f))), 1)  # the header only
        code, printed, _ = self.run_main(self.answer_file(constructed_answer(self.prep)), "--history", str(history))
        self.assertEqual(code, 0)
        self.assertIn(" | model not called: every owing customer was already drafted", printed)

    def test_a_corrupt_history_line_refuses_the_run_with_its_line_number(self):
        good = self.line(self.who, days_before(1))
        record = json.loads(good)
        corrupt = [("not json", "it is not JSON"),
                   ("", "it is not JSON"),
                   ("[]", "it is not a history record"),
                   (json.dumps({k: v for k, v in record.items() if k != "total"}), "it is not a history record"),
                   (json.dumps(dict(record, extra="x")), "it is not a history record"),
                   (json.dumps(dict(record, total=5700)), "every field must be a text"),
                   (json.dumps(dict(record, total="4,200")), "total '4,200' is not a plain decimal number"),
                   (json.dumps(dict(record, customer_key="Maple Bakery")), "customer_key is not a sha256 in hex"),
                   (json.dumps(dict(record, request_sha256="ab" * 31)), "request_sha256 is not a sha256 in hex"),
                   (json.dumps(dict(record, exported_on="24/11/2026")),
                    "exported_on '24/11/2026' is not a date written yyyy-mm-dd"),
                   (json.dumps(dict(record, exported_on="2026-02-30")),
                    "exported_on '2026-02-30' is not a date in the calendar"),
                   (json.dumps(dict(record, exported_on="2026-11-25")),
                    "exported_on 2026-11-25 is after the run's date 2026-11-24")]
        for number, (bad, why) in enumerate(corrupt):
            history = self.history(good, bad, good)
            spy = SpyBackend("{}")
            out = self.base / f"corrupt-{number}"
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.run(SHEET, PROMPT, spy, out, history=history, as_of=AS_OF)
            self.assertEqual((ctx.exception.code, spy.calls), (2, 0), bad)
            self.assertIn(f"the history file, line 2: {why}", str(ctx.exception))
            self.assertFalse(out.exists())
        code, _, err = self.run_main(self.answer_file(constructed_answer(self.prep)), "--history", str(history))
        self.assertEqual(code, 2)
        self.assertIn("REFUSED: the history file, line 2:", err)
        self.assertFalse((self.base / "run").exists())

    def test_the_history_key_is_a_hash_and_holds_no_name(self):
        key = pipeline.customer_key(" Maple Bakery (invented) ", [" 000000001"])
        self.assertEqual(key, pipeline.customer_key("Maple Bakery (invented)", ["000000001"]))  # the ends, as grouped
        self.assertNotEqual(key, pipeline.customer_key("Maple Bakery (invented)", ["000000002"]))
        self.assertNotEqual(key, pipeline.customer_key("Maple Florist (invented)", ["000000001"]))
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        rows, _ = pipeline.load_sheet(SHEET)
        row = next(r for r in rows if r["name"] == self.real_name)
        self.assertEqual(self.prep["customer_keys"][self.who], pipeline.customer_key(row["name"], [row["id_number"]]))

    def test_two_spellings_of_a_name_are_two_customers_in_the_history_too(self):
        sheet = self.write("two.csv", "name,id_number,amount_due,days_late,notes\nAcme Works,000000001,100,5,\n"
                                      "ACME  Works,000000001,100,5,\n")
        prep = pipeline.prepare(sheet, PROMPT)
        keys = prep["customer_keys"]
        self.assertEqual(sorted(prep["key"].values())[:2], ["000000001", "ACME  Works"])
        self.assertNotEqual(keys["Customer 001"], keys["Customer 002"])
        line = json.dumps({"customer_key": keys["Customer 001"], "exported_on": days_before(1), "total": "100",
                           "request_sha256": "ab" * 32})
        records = pipeline.read_history(self.history(line), AS_OF)
        self.assertEqual(list(pipeline.prepare(sheet, PROMPT, records, AS_OF)["already_drafted"]), ["Customer 001"])

    def test_bad_dates_and_windows_are_refused_by_the_command_line(self):
        answer = self.answer_file(constructed_answer(self.prep))
        for args in (["--as-of", "2026-02-30"], ["--as-of", "24-11-2026"], ["--window-days", "0"],
                     ["--window-days", "-1"], ["--window-days", "1.5"]):
            code, _, err = call_main(pipeline, ["--recorded", str(answer), "--out", str(self.base / "o"), *args])
            self.assertEqual(code, 2, args)
            self.assertIn("error: argument", err)
            self.assertFalse((self.base / "o").exists())

    def test_an_answer_for_a_customer_left_out_is_listed_apart_and_not_counted_as_refused(self):
        history = self.history(self.line(self.who, days_before(1)))
        answer = self.answer_file(constructed_answer(self.prep))  # an answer to the full request: it holds them
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(answer), self.base / "r", history=history,
                              as_of=AS_OF)
        self.assertEqual((report["rejected"], report["answered_although_drafted"]), ([], [self.who]))
        self.assertEqual((report["missing_from_answer"], report["accepted"]), ([], len(self.owing) - 1))
        page = (self.base / "r" / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertIn("Answered although already drafted (from an answer to another request; set aside, not counted "
                      f"as refused):\n- {self.real_name}\n", page)
        person = json.loads((self.base / "r" / "for-a-person.json").read_text(encoding="utf-8"))
        self.assertEqual(person["answered_although_drafted"], [self.real_name])

    def test_a_replayed_answer_saved_for_another_request_is_named_in_one_line(self):
        history = self.history(self.line(self.who, days_before(1)))
        code, printed, _ = call_main(pipeline, ["--out", str(self.base / "o"), "--history", str(history),
                                                "--as-of", AS_OF.isoformat()])  # the shipped recording and its meta
        self.assertEqual(code, 0)
        lines = printed.splitlines()
        self.assertIn("| answered although drafted 1", lines[0])
        self.assertEqual(lines[1], "note: the replayed answer was saved for another request (its meta file's "
                                   "request_sha256 is not this run's), so items for customers outside this request "
                                   "are listed apart")
        code, printed, _ = call_main(pipeline, ["--out", str(self.base / "same"), "--as-of", AS_OF.isoformat()])
        self.assertEqual((code, len(printed.splitlines())), (0, 1))  # its own request: no note

    def test_print_request_leaves_the_drafted_customer_out(self):
        history = self.history(self.line(self.who, days_before(1)))
        out = io.BytesIO()
        fake_stdout = io.TextIOWrapper(out, encoding="utf-8")
        with contextlib.redirect_stdout(fake_stdout):
            code = pipeline.main(["--print-request", "--history", str(history), "--as-of", AS_OF.isoformat()])
        fake_stdout.flush()
        self.assertEqual(code, 0)
        printed = out.getvalue().decode("utf-8")
        self.assertNotIn(f"\n{self.who},", printed)
        records = pipeline.read_history(history, AS_OF)
        self.assertEqual(printed, pipeline.prepare(SHEET, PROMPT, records, AS_OF)["request"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
