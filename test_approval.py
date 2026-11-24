"""Tests of the approval step: decisions recorded, appended and refused, and the export of only what was approved.
Run: python -I test_approval.py  (every test file: python -I run_tests.py)"""
import csv
import json
import os
import pathlib
import shutil
import stat
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import pipeline  # noqa: E402
from testsupport import PROMPT, SHEET, TempTest, call_main, constructed_answer  # noqa: E402

AS_OF = "2026-11-24"
AT = "2026-11-24T09:00:00Z"
BY = "Reviewer One (invented)"


class ApprovalTest(TempTest):
    def setUp(self):
        super().setUp()
        self.prep = pipeline.prepare(SHEET, PROMPT)
        self.answer = self.answer_file(constructed_answer(self.prep))
        self.run_dir = self.run_pipeline("run")
        self.report = json.loads((self.run_dir / "run-report.json").read_text(encoding="utf-8"))
        self.history = self.base / "history.jsonl"

    def run_pipeline(self, name, *extra):
        out = self.base / name
        code, _, err = call_main(pipeline, ["--sheet", str(SHEET), "--recorded", str(self.answer), "--out", str(out),
                                            "--as-of", AS_OF, *extra])
        self.assertEqual(code, 0, err)
        return out

    def decide(self, command, item, *extra, run=None):
        return call_main(approval, [command, str(run or self.run_dir), "--item", str(item), "--by", BY, "--at", AT,
                                    *extra])

    def export(self, *extra, run=None):
        return call_main(approval, ["export", str(run or self.run_dir), "--history", str(self.history),
                                    "--as-of", AS_OF, *extra])

    def decisions(self):
        return json.loads((self.run_dir / "decisions.json").read_text(encoding="utf-8"))

    def assert_refused_and_nothing_exported(self, result, message):
        code, _, err = result
        self.assertEqual(code, 2, err)
        self.assertIn(message, err)
        self.assertFalse((self.run_dir / "approved").exists())
        self.assertFalse(self.history.exists())

    def test_every_draft_starts_pending(self):
        code, printed, _ = call_main(approval, ["list", str(self.run_dir)])
        self.assertEqual(code, 0)
        lines = printed.splitlines()
        drafts = len(self.report["drafts"])
        self.assertEqual(len(lines), drafts + 1)
        self.assertTrue(all(line.endswith("| pending") for line in lines[:-1]))
        self.assertEqual(lines[-1], f"{drafts} draft(s): 0 approved, 0 rejected, {drafts} pending")
        first = self.report["drafts"][0]
        self.assertEqual(lines[0], f"item 1 | {self.prep['key'][first['customer']]} | {first['amount']} | "
                                   f"{first['bucket']} | pending")
        self.assertFalse((self.run_dir / "decisions.json").exists(), "listing records nothing")

    def test_show_prints_the_draft_being_decided(self):
        first = self.report["drafts"][0]
        reminder = (self.run_dir / first["file"]).read_text(encoding="utf-8").rstrip("\n")
        code, printed, _ = call_main(approval, ["show", str(self.run_dir), "--item", "1"])
        self.assertEqual(code, 0)
        lines = printed.splitlines()
        self.assertEqual(lines[0], f"item 1 | {self.prep['key'][first['customer']]} | {first['amount']} | "
                                   f"{first['days_late']} days late | {first['bucket']} | pending")
        self.assertTrue(lines[1].startswith("reason: A balance of "), lines[1])
        self.assertEqual(lines[2:], ["reminder (reminders/reminder-01.txt):"] + reminder.splitlines())
        self.decide("approve", 1)
        self.assertIn(f"| approved by {BY} at {AT}", call_main(approval, ["show", str(self.run_dir), "--item", "1"])[1])
        code, _, err = call_main(approval, ["show", str(self.run_dir), "--item", "999"])
        self.assertEqual(code, 2)
        self.assertIn("this run has no draft with item 999", err)

    def test_the_page_for_a_person_ends_with_a_key_of_the_customers(self):
        page = (self.run_dir / "for-a-person.txt").read_text(encoding="utf-8")
        key = page.split("Key of this run's customers (stand-in: name), to read a text that names a stand-in:\n")[1]
        customers = {s: n for s, n in self.prep["key"].items() if s.startswith("Customer ")}
        self.assertEqual(key.splitlines(), [f"- {s}: {customers[s]}" for s in sorted(customers)])
        person = json.loads((self.run_dir / "for-a-person.json").read_text(encoding="utf-8"))
        self.assertEqual(person["customers"], customers)

    def test_approvals_and_rejections_are_recorded_with_who_when_and_why(self):
        self.assertEqual(self.decide("approve", 1)[0], 0)
        code, printed, _ = self.decide("reject", 2, "--reason", "  The owner calls this customer first  ")
        self.assertEqual((code, printed), (0, f"item 2: rejected by {BY} at {AT}: The owner calls this customer "
                                              "first\n"))
        data = self.decisions()
        self.assertEqual(data["request_sha256"], self.report["request_sha256"])
        self.assertEqual(data["drafts_sha256"], approval.drafts_digest(self.report["drafts_sha256"]))
        self.assertEqual(data["decisions"], [
            {"item": 1, "decision": "approved", "by": BY, "at": AT, "reason": None},
            {"item": 2, "decision": "rejected", "by": BY, "at": AT, "reason": "The owner calls this customer first"}])
        lines = call_main(approval, ["list", str(self.run_dir)])[1].splitlines()
        self.assertTrue(lines[0].endswith(f"| approved by {BY} at {AT}"))
        self.assertTrue(lines[1].endswith(f"| rejected by {BY} at {AT}: The owner calls this customer first"))
        self.assertIn("1 approved, 1 rejected,", lines[-1])

    def test_without_at_the_time_is_now_in_utc(self):
        code, _, _ = call_main(approval, ["approve", str(self.run_dir), "--item", "1", "--by", BY])
        self.assertEqual(code, 0)
        self.assertRegex(self.decisions()["decisions"][0]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_decisions_are_appended_and_the_last_one_counts(self):
        self.decide("approve", 1)
        first = self.decisions()["decisions"]
        self.decide("reject", 1, "--reason", "wrong tone")
        self.assertEqual(self.decisions()["decisions"][:1], first, "an earlier entry is never rewritten")
        self.assertEqual(approval.states(self.report, self.decisions())[1]["decision"], "rejected")
        self.decide("approve", 1)
        self.assertEqual([e["decision"] for e in self.decisions()["decisions"]], ["approved", "rejected", "approved"])
        code, printed, _ = self.export()
        self.assertEqual(code, 0)
        self.assertIn("exported items 1 to approved/", printed)

    def test_a_decision_needs_a_known_item_a_name_a_reason_and_a_real_timestamp(self):
        run = str(self.run_dir)
        cases = [(["approve", run, "--item", "999", "--by", BY], "this run has no draft with item 999"),
                 (["approve", run, "--item", "1", "--by", "   "], "--by must name the person who decides"),
                 (["reject", run, "--item", "1", "--by", BY, "--reason", "   "], "a rejection needs a reason"),
                 (["approve", run, "--item", "1", "--by", BY, "--at", "2026-11-24 09:00"], "is not a UTC timestamp"),
                 (["approve", run, "--item", "1", "--by", BY, "--at", "2026-13-01T00:00:00Z"],
                  "is not a UTC timestamp")]
        for argv, message in cases:
            code, _, err = call_main(approval, argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err)
        code, _, err = call_main(approval, ["reject", str(self.run_dir), "--item", "1", "--by", BY])
        self.assertEqual(code, 2)
        self.assertIn("--reason", err)  # argparse: the reason is required
        self.assertFalse((self.run_dir / "decisions.json").exists(), "nothing was recorded")

    def test_export_writes_only_the_approved_drafts_and_the_history(self):
        self.decide("approve", 1)
        self.decide("approve", 3)
        self.decide("reject", 2, "--reason", "disputed")
        report_before = (self.run_dir / "run-report.json").read_bytes()
        code, printed, _ = self.export()
        self.assertEqual(code, 0)
        pending = len(self.report["drafts"]) - 3
        self.assertEqual(printed, f"exported items 1, 3 to approved/ | rejected 1 | pending {pending} | history "
                                  "lines added 2 | sent to customers 0\n")
        approved = self.run_dir / "approved"
        self.assertEqual(sorted(p.name for p in (approved / "reminders").iterdir()),
                         ["reminder-01.txt", "reminder-03.txt"])
        for name in ("reminder-01.txt", "reminder-03.txt"):
            self.assertEqual((approved / "reminders" / name).read_bytes(),
                             (self.run_dir / "reminders" / name).read_bytes())
        with (approved / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["priority", "customer", "amount", "days_late", "bucket", "reason", "approved_by",
                                   "approved_at"])
        self.assertEqual([r[0] for r in rows[1:]], ["1", "3"])
        self.assertEqual(rows[1][-2:], [BY, AT])
        lines = [json.loads(line) for line in self.history.read_text(encoding="utf-8").splitlines()]
        drafts = {d["item"]: d for d in self.report["drafts"]}
        self.assertEqual(lines, [{"customer_key": drafts[i]["customer_key"], "exported_on": AS_OF,
                                  "total": drafts[i]["amount"], "request_sha256": self.report["request_sha256"]}
                                 for i in (1, 3)])
        history_text = self.history.read_text(encoding="utf-8")
        for value in pipeline.personal_values(pipeline.load_sheet(SHEET)[0]):
            self.assertNotIn(value, history_text)
        self.assertEqual((self.run_dir / "run-report.json").read_bytes(), report_before)
        self.assertEqual(json.loads(report_before)["sent_to_customers"], 0)  # there is still no send step

    def test_an_export_with_nothing_approved_exports_nothing_and_is_not_an_error(self):
        code, printed, _ = self.export()
        self.assertEqual(code, 0)
        self.assertTrue(printed.startswith("nothing approved: nothing exported | rejected 0 |"))
        self.decide("reject", 1, "--reason", "not now")
        code, printed, _ = self.export()
        self.assertEqual(code, 0)
        self.assertTrue(printed.startswith("nothing approved: nothing exported | rejected 1 |"))
        self.assertFalse((self.run_dir / "approved").exists())
        self.assertFalse(self.history.exists())

    def test_export_refuses_decisions_that_name_another_run(self):
        other = self.run_pipeline("other", "--history", str(self.write("h.jsonl", "")), "--window-days", "3")
        self.decide("approve", 1, run=other)  # same request, same drafts: other is the same run in a second folder
        shutil.copyfile(other / "decisions.json", self.run_dir / "decisions.json")
        self.assertEqual(self.export()[0], 0, "identical drafts are not another run")
        shutil.rmtree(self.run_dir / "approved")
        self.history.unlink()
        for field in ("request_sha256", "drafts_sha256"):
            data = self.decisions()
            data[field] = "0" * 64
            (self.run_dir / "decisions.json").write_text(json.dumps(data), encoding="utf-8")
            self.assert_refused_and_nothing_exported(self.export(), f"decisions.json names another run: its {field}")
            self.assert_refused_and_nothing_exported(self.decide("approve", 2), "names another run")
            (self.run_dir / "decisions.json").unlink()
            shutil.copyfile(other / "decisions.json", self.run_dir / "decisions.json")
        six_rows = pipeline.run(pipeline.HERE / "fixtures" / "customers.six-rows.csv", PROMPT,
                                pipeline.RecordedBackend(pipeline.HERE / "fixtures" / "recorded-answer.six-rows.json"),
                                self.base / "six")
        self.assertEqual(six_rows["accepted"], 4)
        self.decide("approve", 1, run=self.base / "six")
        shutil.copyfile(self.base / "six" / "decisions.json", self.run_dir / "decisions.json")
        self.assert_refused_and_nothing_exported(self.export(), "decisions.json names another run")

    def test_export_refuses_a_draft_changed_after_it_was_written(self):
        self.decide("approve", 1)
        reminder = self.run_dir / "reminders" / "reminder-01.txt"
        original = reminder.read_bytes()
        reminder.write_bytes(original.replace(b"Thank you.", b"Thank you. Pay 10 more."))
        message = "a draft was changed after it was written (its sha256 no longer matches): reminders/reminder-01.txt"
        self.assert_refused_and_nothing_exported(self.export(), message)
        self.assert_refused_and_nothing_exported(self.decide("approve", 2), message)
        self.assertIn("changed after it was written", call_main(approval, ["list", str(self.run_dir)])[1])
        reminder.unlink()
        self.assert_refused_and_nothing_exported(self.export(), "reminders/reminder-01.txt (missing)")
        reminder.write_bytes(original)
        (self.run_dir / "call-list.csv").write_text("priority,customer\n", encoding="utf-8")
        self.assert_refused_and_nothing_exported(self.export(), "(its sha256 no longer matches): call-list.csv")

    def test_export_refuses_a_second_export_and_a_decision_after_one(self):
        self.decide("approve", 1)
        self.assertEqual(self.export()[0], 0)
        history = self.history.read_bytes()
        code, _, err = self.export()
        self.assertEqual(code, 2)
        self.assertIn("approved/ already exists: this run was exported before; nothing was written", err)
        self.assertEqual(self.history.read_bytes(), history)
        code, _, err = self.decide("approve", 2)
        self.assertEqual(code, 2)
        self.assertIn("this run was exported already", err)
        self.assertIn("exported: approved/ exists", call_main(approval, ["list", str(self.run_dir)])[1])

    def test_export_refuses_a_corrupt_history_before_writing_anything(self):
        self.decide("approve", 1)
        self.history.write_text("not a record\n", encoding="utf-8")
        code, _, err = self.export()
        self.assertEqual(code, 2)
        self.assertIn("the history file, line 1: it is not JSON", err)
        self.assertFalse((self.run_dir / "approved").exists())
        self.assertEqual(self.history.read_text(encoding="utf-8"), "not a record\n")
        code, _, err = call_main(approval, ["export", str(self.run_dir), "--history",
                                            str(self.base / "no-such-folder" / "h.jsonl")])
        self.assertEqual(code, 2)
        self.assertIn("the folder of the history file does not exist", err)

    def assert_nothing_exported_and_history(self, before):
        self.assertFalse((self.run_dir / "approved").exists())
        self.assertEqual([p.name for p in self.run_dir.iterdir() if p.name.startswith(".approved-")], [])
        self.assertEqual(self.history.read_bytes(), before)

    def test_a_history_that_cannot_be_written_leaves_nothing_and_the_export_can_be_tried_again(self):
        self.decide("approve", 1)
        self.history.write_bytes(b"")
        os.chmod(self.history, stat.S_IREAD)
        try:
            if os.access(self.history, os.W_OK):
                self.skipTest("this account can write a read-only file")
            code, _, err = self.export()
        finally:
            os.chmod(self.history, stat.S_IREAD | stat.S_IWRITE)
        self.assertEqual(code, 2)
        self.assertIn("the export could not be completed", err)
        self.assert_nothing_exported_and_history(b"")
        self.assertEqual(self.export()[0], 0)
        self.assertTrue((self.run_dir / "approved" / "reminders" / "reminder-01.txt").is_file())
        self.assertEqual(len(self.history.read_text(encoding="utf-8").splitlines()), 1)

    def test_a_failure_after_the_history_was_appended_puts_it_back(self):
        self.decide("approve", 1)
        earlier = self.report["drafts"][1]
        self.history.write_text(json.dumps({"customer_key": earlier["customer_key"], "exported_on": "2026-11-01",
                                            "total": earlier["amount"], "request_sha256": "ab" * 32}) + "\n",
                                encoding="utf-8")
        before = self.history.read_bytes()
        with mock.patch.object(approval, "move_into_place", side_effect=OSError("the disk is full")):
            code, _, err = self.export()
        self.assertEqual(code, 2)
        self.assertIn("(the disk is full); nothing was exported, the history is as it was", err)
        self.assert_nothing_exported_and_history(before)
        self.assertEqual(self.export()[0], 0)
        self.assertEqual(len(self.history.read_text(encoding="utf-8").splitlines()), 2)

    def test_a_customer_already_exported_inside_the_window_is_not_exported_again(self):
        second, third = self.run_pipeline("second"), self.run_pipeline("third")  # the same drafts, before any export
        for item in (1, 2):
            self.decide("approve", item)
        self.decide("approve", 1, run=second)
        self.decide("approve", 1, run=third)
        self.decide("approve", 3, run=third)
        self.assertEqual(self.export()[0], 0)
        pending = len(self.report["drafts"]) - 1
        code, printed, _ = self.export(run=second)
        self.assertEqual((code, printed), (0, f"nothing exported: every approved item was already exported | "
                                              f"rejected 0 | pending {pending} | already exported: item 1 on {AS_OF} "
                                              f"| history lines added 0 | sent to customers 0\n"))
        self.assertFalse((second / "approved").exists())
        code, printed, _ = self.export(run=third)
        self.assertEqual(code, 0)
        self.assertTrue(printed.startswith("exported items 3 to approved/ |"))
        self.assertIn(f"| already exported: item 1 on {AS_OF} | history lines added 1 |", printed)
        self.assertEqual(sorted(p.name for p in (third / "approved" / "reminders").iterdir()), ["reminder-03.txt"])
        # Seven days later the first export is outside the window: the same customer is exported again.
        later = self.run_pipeline("later")
        self.decide("approve", 1, run=later)
        code, printed, _ = call_main(approval, ["export", str(later), "--history", str(self.history),
                                                "--as-of", "2026-12-01"])
        self.assertEqual(code, 0)
        self.assertTrue(printed.startswith("exported items 1 to approved/ |"), printed)

    def test_a_run_report_that_names_a_file_outside_the_run_is_refused(self):
        for name in ("../outside.txt", "reminders/../../outside.txt", "notes.txt"):
            data = dict(self.report, drafts_sha256=dict(self.report["drafts_sha256"], **{name: "0" * 64}))
            (self.run_dir / "run-report.json").write_text(json.dumps(data), encoding="utf-8")
            code, _, err = call_main(approval, ["list", str(self.run_dir)])
            self.assertEqual(code, 2, name)
            self.assertIn(f"run-report.json names an unexpected draft file: {name!r}", err)

    def test_an_export_appends_after_a_last_line_without_a_line_end(self):
        self.decide("approve", 1)
        earlier = self.report["drafts"][1]
        line = json.dumps({"customer_key": earlier["customer_key"], "exported_on": "2026-11-01",
                           "total": earlier["amount"], "request_sha256": "ab" * 32})
        self.history.write_text(line, encoding="utf-8")  # no line end after the last line
        self.assertEqual(self.export()[0], 0)
        self.assertEqual(len(pipeline.read_history(self.history, pipeline.iso_date(AS_OF))), 2)

    def test_the_next_run_leaves_out_what_was_exported(self):
        for item in (1, 2):
            self.decide("approve", item)
        self.assertEqual(self.export()[0], 0)
        drafts = {d["item"]: d["customer"] for d in self.report["drafts"]}
        records = pipeline.read_history(self.history, pipeline.iso_date(AS_OF))
        prep = pipeline.prepare(SHEET, PROMPT, records, pipeline.iso_date(AS_OF))
        self.assertEqual(sorted(prep["already_drafted"]), sorted([drafts[1], drafts[2]]))
        self.answer = self.answer_file(constructed_answer(prep))
        second = self.run_pipeline("second", "--history", str(self.history))
        report = json.loads((second / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["already_drafted"]), 2)
        self.assertEqual(report["accepted"], len(self.report["drafts"]) - 2)
        self.assertNotIn(drafts[1], [d["customer"] for d in report["drafts"]])

    def test_a_folder_that_is_not_a_run_and_a_broken_decisions_file_are_refused(self):
        empty = self.base / "empty"
        empty.mkdir()
        for argv in (["list", str(empty)], ["export", str(empty)],
                     ["approve", str(empty), "--item", "1", "--by", BY]):
            code, _, err = call_main(approval, argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("not a run folder", err)
        self.decide("approve", 1)
        data = self.decisions()
        for broken, message in [(dict(data, decisions=[{"item": 1}]), "decision 1: it is not a decision"),
                                (dict(data, decisions=[dict(data["decisions"][0], decision="maybe")]),
                                 "decision 1: decision 'maybe' is neither approved nor rejected"),
                                (dict(data, decisions=[dict(data["decisions"][0], decision="rejected")]),
                                 "decision 1: a rejection has no reason"),
                                (dict(data, decisions=[dict(data["decisions"][0], item=999)]),
                                 "decision 1: item 999 is not a draft of this run"),
                                (dict(data, decisions=[dict(data["decisions"][0], by="   ")]),
                                 "decision 1: by is empty"),
                                (dict(data, decisions=[dict(data["decisions"][0], at="2026-99-24T09:00:00Z")]),
                                 "decision 1: at is not a UTC timestamp")]:
            (self.run_dir / "decisions.json").write_text(json.dumps(broken), encoding="utf-8")
            self.assert_refused_and_nothing_exported(self.export(), message)


if __name__ == "__main__":
    unittest.main(verbosity=1)
