"""Tests of the checks added after an independent review: loose references to stand-ins, what a reminder may not
hold, the sheet check's limits, the leak guard's whole words, and the amounts kept exact after the scrub.
Run: python -I test_checks.py  (every test file: python -I run_tests.py)"""
import csv
import http.server
import json
import os
import pathlib
import sys
import threading
import unittest
from decimal import Decimal
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import make_sheet  # noqa: E402
import pipeline  # noqa: E402
import pseudonymise  # noqa: E402
from testsupport import (PROMPT, SHEET, MockServer, SpyBackend, TempTest, call_main,  # noqa: E402
                         closed_port_url, constructed_answer)


class TextCheckTest(unittest.TestCase):
    """Every checked text: the note for a person, the reason and the reminder."""

    def setUp(self):
        self.prep = pipeline.prepare(SHEET, PROMPT)
        self.answer = constructed_answer(self.prep)
        self.first = self.answer["call_list"][0]
        self.c = self.prep["expected"][self.first["customer"]]

    def check(self, answer):
        """(rejection reasons, rejected-note reasons) of an answer, read as the pipeline reads a model's answer."""
        _, rejected, _, _, rejected_notes = pipeline.validate(pipeline.parse_answer(json.dumps(answer)),
                                                              self.prep["expected"], self.prep["key"])
        return [r["reason"] for r in rejected], [r["reason"] for r in rejected_notes]

    def with_first(self, **changes):
        answer = json.loads(json.dumps(self.answer))
        answer["call_list"][0].update(changes)
        return answer

    def test_a_loose_reference_to_stand_ins_is_set_aside_in_a_note(self):
        # 5, 7 and 16 are figures in the sheet, so without this check the note passes by luck.
        sheet_figures = set().union(*(c["figures"] for c in self.prep["expected"].values()))
        self.assertTrue({Decimal(5), Decimal(7), Decimal(16)} <= sheet_figures)
        note = "Customers 005, 007 and 016 promised to pay by the end of the month."
        self.assertEqual(self.check(dict(self.answer, for_a_human=[note]))[1],
                         ["the note holds a loose reference to a stand-in: 005, 007, 016"])
        written_in_full = "Customer 005, Customer 007 and Customer 016 promised to pay by the end of the month."
        self.assertEqual(self.check(dict(self.answer, for_a_human=[written_in_full])), ([], []))

    def test_a_loose_reference_is_refused_in_a_reason_and_in_a_reminder(self):
        who = self.first["customer"]
        reason = self.first["reason"] + " Like 005."
        self.assertEqual(self.check(self.with_first(reason=reason))[0],
                         ["the reason holds a loose reference to a stand-in: 005"])
        reminder = self.first["reminder"].replace(who, who + " (account 012)")
        self.assertEqual(self.check(self.with_first(reminder=reminder))[0],
                         ["the reminder holds a loose reference to a stand-in: 012"])

    def test_a_leading_zero_inside_a_decimal_a_thousands_group_or_a_stand_in_is_not_loose(self):
        self.assertEqual(pipeline.LOOSE_STAND_IN.findall(pipeline.without_tokens(
            "0.05, 1,050, 0 and Customer 005", {"Customer 005"})), [])

    def test_a_reminder_may_not_hold_a_per_cent_sign_or_a_web_address(self):
        reminder = self.first["reminder"]
        self.assertEqual(self.check(self.with_first(reminder=reminder + " A late fee of 5% applies."))[0],
                         ["the reminder holds a per cent sign"])
        for address in ("https://pay.example.com/now", "www.example.com", "pay.example.com"):
            self.assertEqual(self.check(self.with_first(reminder=f"{reminder} Pay at {address}"))[0],
                             [f"the reminder holds a web address: {address}"], address)

    def test_a_numeral_other_than_0_to_9_is_refused_everywhere(self):
        circled_one = chr(0x2460)
        total = pipeline.money(self.c["total"])
        self.assertTrue(total.isdigit(), total)
        full_width = "".join(chr(0xFF10 + int(d)) for d in total)
        # A total in full-width digits reads as the right figure to a pattern that takes any digit: it must not pass.
        reminder = f"Dear {self.first['customer']}, please pay {full_width}."
        reasons = self.check(self.with_first(reminder=reminder))[0]
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("the reminder holds a numeral other than 0 to 9: U+FF1"), reasons)
        self.assertEqual(self.check(self.with_first(reason="first call " + circled_one))[0],
                         ["the reason holds a numeral other than 0 to 9: U+2460"])
        self.assertEqual(self.check(dict(self.answer, for_a_human=["call " + circled_one + " first"]))[1],
                         ["the note holds a numeral other than 0 to 9: U+2460"])


class SheetCheckTest(TempTest):
    def refused(self, text):
        spy = SpyBackend("{}")
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run(self.write("sheet.csv", text), PROMPT, spy, self.base / "out")
        self.assertEqual((ctx.exception.code, spy.calls), (2, 0))
        self.assertFalse((self.base / "out").exists())
        return str(ctx.exception)

    def test_an_amount_with_more_than_15_digits_before_the_point_is_refused(self):
        head = "name,amount_due,days_late,notes\n"
        self.assertIn("row 2 of the sheet: amount_due '1234567890123456' has more than 15 digits before the point",
                      self.refused(head + "Big Shop,1234567890123456,1,\n"))
        prep = pipeline.prepare(self.write("ok.csv", head + "Big Shop,123456789012345.67,1,\n"), PROMPT)
        self.assertEqual(prep["expected"]["Customer 001"]["total"], Decimal("123456789012345.67"))

    def test_a_header_that_differs_only_in_letter_case_is_refused_with_the_expected_name(self):
        self.assertIn("the sheet has no column 'name' (it has 'Name'; column names must match exactly)",
                      self.refused("Name,amount_due,days_late,notes\nA B,5,1,\n"))
        self.assertIn("the sheet has a column 'Phone' where 'phone' is expected (column names must match exactly)",
                      self.refused("name,Phone,amount_due,days_late,notes\nA B,000-000-0001,5,1,\n"))

    def test_the_leak_guard_matches_whole_words_as_the_scrubber_does(self):
        head = "name,amount_due,days_late,notes\n"
        # "Al" is inside "total" in the prompt, but it is not in the request as a word: not a leak.
        request = pipeline.prepare(self.write("al.csv", head + "Al,5,1,\n"), PROMPT)["request"]
        self.assertIn("total", request)
        self.assertIn("\nCustomer 001,5,1,\n", request)
        # A real leak is still caught: the name broken by a line break is joined in the request.
        self.assertIn("1 personal value(s) would reach the model",
                      self.refused(head + 'Alpha Shop,5,1,"Ask Alpha\nShop again"\n'))

    def test_the_amounts_are_restored_after_the_scrub(self):
        # A nine-digit amount looks like an identity number to the scrub of the notes' patterns.
        sheet = self.write("sheet.csv", "name,amount_due,days_late,notes\nBig Shop,123456789,1,\n")
        prep = pipeline.prepare(sheet, PROMPT)
        self.assertIn("\nCustomer 001,123456789,1,\n", prep["request"])
        self.assertEqual(prep["expected"]["Customer 001"]["total"], Decimal("123456789"))


class FormulaCellTest(TempTest):
    """A cell a spreadsheet could run as a formula is written with a leading apostrophe in every CSV file."""

    SHEET_TEXT = "name,amount_due,days_late,notes\n=SUM(A1) Shop,100,5,\n@Second Shop,50,2,\n"

    def run_formula_sheet(self):
        answer = {"call_list": [
            {"customer": "Customer 001", "priority": 1, "amount": 100, "days_late": 5, "reason": "-- call first",
             "reminder": "Dear Customer 001, the balance of 100 is 5 days late."},
            {"customer": "Customer 002", "priority": 2, "amount": 50, "days_late": 2, "reason": "smaller",
             "reminder": "Dear Customer 002, the balance of 50 is 2 days late."}], "for_a_human": []}
        out = self.base / "run"
        pipeline.run(self.write("sheet.csv", self.SHEET_TEXT), PROMPT,
                     pipeline.RecordedBackend(self.answer_file(answer)), out)
        return out

    def test_a_name_and_a_reason_that_start_like_a_formula_are_written_as_text(self):
        out = self.run_formula_sheet()
        with (out / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1], ["1", "'=SUM(A1) Shop", "100", "5", "1-30", "'-- call first"])
        self.assertEqual(rows[2][1], "'@Second Shop")
        page = (out / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertIn("does not run them as formulas: 3\n", page)
        self.assertEqual(json.loads((out / "for-a-person.json").read_text(encoding="utf-8"))["csv_cells_guarded"], 3)

    def test_a_by_that_starts_like_a_formula_is_refused_and_the_export_adds_no_second_apostrophe(self):
        out = self.run_formula_sheet()
        for by in ("=1+1", "+1", "-x", "@x", "\t=x", " =x"):
            code, _, err = call_main(approval, ["approve", str(out), "--item", "1", f"--by={by}"])
            self.assertEqual(code, 2, repr(by))
            self.assertIn("--by may not start with =, +, -, @, a tab or a carriage return", err)
        self.assertFalse((out / "decisions.json").exists())
        code, _, _ = call_main(approval, ["approve", str(out), "--item", "1", "--by", "Ann-Marie (invented)"])
        self.assertEqual(code, 0)
        self.assertEqual(call_main(approval, ["export", str(out)])[0], 0)
        with (out / "approved" / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            row = list(csv.reader(f))[1]
        self.assertEqual(row[:2] + row[5:7], ["1", "'=SUM(A1) Shop", "'-- call first", "Ann-Marie (invented)"])

    def test_the_pseudonymised_and_the_generated_sheets_are_guarded_too(self):
        source = self.write("source.csv", "name,amount_due,days_late,notes\nA Shop,5,1,=cmd|' /C calc'!A0\n")
        out, key = self.base / "clean.csv", self.base / "key.json"
        code, printed, _ = call_main(pseudonymise, [str(source), "--out", str(out), "--key", str(key)])
        self.assertEqual(code, 0)
        self.assertIn("| cells written with a leading apostrophe 1", printed)
        self.assertIn("Customer 001,5,1,'=cmd|' /C calc'!A0", out.read_text(encoding="utf-8-sig"))
        row = dict(zip(make_sheet.COLUMNS, ["+Plus Shop (invented)", "000000001", "000-000-0001",
                                            "plus@example.com", "1 Example Street Example City", "5", "1", ""]))
        self.assertIn("\n'+Plus Shop (invented),000000001,", make_sheet.sheet_text([row]))


class Redirect(http.server.BaseHTTPRequestHandler):
    """Answers every POST with a redirect to the address set on the server."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(302)
        self.send_header("location", self.server.target)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class WebhookTest(TempTest):
    """The webhook reaches the address it was given, and nothing else."""

    def run_with_webhook(self, url, name):
        answer = self.answer_file(constructed_answer(pipeline.prepare(SHEET, PROMPT)))
        return call_main(pipeline, ["--recorded", str(answer), "--out", str(self.base / name), "--webhook", url])

    def test_a_redirect_is_a_failure_and_is_not_followed(self):
        final = MockServer("")
        start = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        start.target = final.url + "/elsewhere"
        threading.Thread(target=start.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            code, printed, _ = self.run_with_webhook(f"http://127.0.0.1:{start.server_address[1]}/hook", "r")
        finally:
            start.shutdown()
            start.server_close()
            final.close()
        self.assertEqual(code, 4)
        self.assertIn("| webhook failed: HTTP 302", printed)
        self.assertEqual(final.httpd.seen, [])

    def test_a_proxy_in_the_environment_is_not_used(self):
        server = MockServer("")
        proxy = closed_port_url()  # were the proxy used, the request would fail here
        try:
            with mock.patch.dict(os.environ, {"HTTP_PROXY": proxy, "http_proxy": proxy, "NO_PROXY": "",
                                              "no_proxy": ""}):
                code, printed, _ = self.run_with_webhook(server.url + "/hook", "p")
        finally:
            server.close()
        self.assertEqual(code, 0, printed)
        self.assertIn("| webhook 200", printed)
        self.assertEqual(len(server.httpd.seen), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
