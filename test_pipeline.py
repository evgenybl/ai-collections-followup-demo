"""Tests of the pipeline and the pseudonymiser, on the synthetic sheet beside them and on small sheets written by the
tests themselves. No network outside this computer: the API and the webhook are local mock servers.
Run: python -I test_pipeline.py"""
import contextlib
import csv
import hashlib
import http.server
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline  # noqa: E402
import pseudonymise  # noqa: E402

SHEET = HERE / "customers.synthetic.csv"
PROMPT = HERE / "prompt.txt"
RECORDED = HERE / "recorded-answer.json"
META = HERE / "recorded-answer.meta.json"
TEST_KEY = "unit" + "-" + "test" + "-" + "placeholder"


def raw_rows():
    rows, _ = pipeline.load_sheet(SHEET)
    return rows


def good_answer():
    """A correct answer written for the tests (not a model's): every figure matches the sheet."""
    return {"call_list": [
        {"customer": "Customer 003", "priority": 1, "amount": 12800, "days_late": 90,
         "reason": "largest and oldest; Customer 003 does not answer the phone",
         "reminder": "Hello Customer 003, we would be glad to settle the balance of 12,800, which is 90 days late."},
        {"customer": "Customer 005", "priority": 2, "amount": 7300, "days_late": 61, "reason": "large, disputes the invoice",
         "reminder": "Hello Customer 005, the open balance is 7300."},
        {"customer": "Customer 001", "priority": 3, "amount": 5700, "days_late": 45, "reason": "two invoices",
         "reminder": "Hello Customer 001, the total balance is 5,700."},
        {"customer": "Customer 002", "priority": 4, "amount": 950, "days_late": 12, "reason": "small, asked to split",
         "reminder": "Hello Customer 002, the balance is 950."},
    ], "for_a_human": ["Customer 005 says the invoice never arrived."]}


def call_main(module, args):
    """Run a command line in this process and return (exit code, stdout, stderr), so nothing reaches the console."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(args)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def api_key(value):
    old = os.environ.get(pipeline.API_KEY_ENV)
    os.environ[pipeline.API_KEY_ENV] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(pipeline.API_KEY_ENV, None)
        else:
            os.environ[pipeline.API_KEY_ENV] = old


def closed_port_url():
    """A local address where nothing listens, for a connection that fails at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class SpyBackend:
    """A backend that counts its calls; it proves that a refused run never reached the model."""

    name, model = "spy", "spy"

    def __init__(self, text=""):
        self.text, self.calls = text, 0

    def complete(self, request_text):
        self.calls += 1
        return self.text


class Mock(http.server.BaseHTTPRequestHandler):
    """Records each request; answers in the Messages API's shape, or with the status and raw body set on the server."""

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length).decode("utf-8")
        self.server.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        out = self.server.raw
        if out is None:
            reply = {"id": "msg_test", "type": "message", "role": "assistant", "model": "mock",
                     "content": [{"type": "text", "text": self.server.reply_text}],
                     "stop_reason": self.server.stop_reason}
            out = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        self.send_response(self.server.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class MockServer:
    def __init__(self, reply_text="", status=200, raw=None, stop_reason="end_turn"):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        self.httpd.seen = []
        self.httpd.reply_text, self.httpd.status, self.httpd.raw = reply_text, status, raw
        self.httpd.stop_reason = stop_reason
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TempTest(unittest.TestCase):
    """A test with its own temporary folder, outside the repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.base / name
        path.write_text(text, encoding="utf-8")
        return path

    def answer_file(self, answer):
        return self.write("answer.json", json.dumps(answer, ensure_ascii=False))


class RequestTest(unittest.TestCase):
    def test_the_model_request_carries_no_personal_value(self):
        text = pipeline.prepare(SHEET, PROMPT)["request"]
        for value in pipeline.personal_values(raw_rows()):
            self.assertNotIn(value.lower(), text.lower())
        self.assertIn("12800", text)
        self.assertIn("\n\nThe table:\n\nname,amount_due,days_late,notes\n", text)
        self.assertNotIn("id_number", text)

    def test_only_the_four_needed_columns_are_sent(self):
        clean = pipeline.prepare(SHEET, PROMPT)["clean"]
        header = pipeline.model_table(clean).splitlines()[0]
        self.assertEqual(header.split(","), pipeline.MODEL_COLUMNS)

    def test_the_model_table_is_written_by_the_csv_module(self):
        rows = [{"name": "Customer 001", "amount_due": "5", "days_late": "1",
                 "notes": 'Said "next week", then\nhung up\r\nagain'}]
        table = pipeline.model_table(rows)
        self.assertNotIn("\r", table)
        self.assertEqual(table.count("\n"), 2)  # the header and one row, each ending in a line feed
        parsed = list(csv.reader(io.StringIO(table)))
        self.assertEqual(parsed[1], ["Customer 001", "5", "1", 'Said "next week", then hung up again'])

    def test_print_request_prints_the_exact_request_and_needs_no_out(self):
        proc = subprocess.run([sys.executable, "-I", "-B", str(HERE / "pipeline.py"), "--print-request"],
                              capture_output=True, timeout=60)
        request = pipeline.prepare(SHEET, PROMPT)["request"]
        self.assertEqual((proc.returncode, proc.stdout), (0, request.encode("utf-8")))
        code, _, err = call_main(pipeline, [])
        self.assertEqual(code, 2)
        self.assertIn("--out is required", err)

    def test_the_recorded_answer_meta_file_matches_the_request(self):
        meta = json.loads(META.read_text(encoding="utf-8"))
        self.assertEqual(sorted(meta), ["model", "recorded_on", "request_sha256", "route"])
        request = pipeline.prepare(SHEET, PROMPT)["request"]
        self.assertEqual(meta["request_sha256"], hashlib.sha256(request.encode("utf-8")).hexdigest(),
                         "the prompt or the sheet changed since the answer was recorded: record it again")


class ScrubTest(TempTest):
    def test_the_three_patterns_catch_values_outside_their_columns(self):
        rows = [{"name": "Some One", "notes": "Call 000-000-0099, write to other@example.com, ID number 900000099"}]
        clean, _ = pseudonymise.pseudonymise(rows, ["name", "notes"])
        self.assertEqual(clean[0]["notes"], "Call [phone removed], write to [e-mail removed], ID number [ID removed]")

    def test_known_values_are_replaced_as_whole_words_in_any_letter_case(self):
        rows = [{"name": "Ann Example", "notes": "Annual review. ANN EXAMPLE called."},
                {"name": "Omer Sample", "notes": "omer sample paid; Omer Samples did not"}]
        clean, _ = pseudonymise.pseudonymise(rows, ["name", "notes"])
        self.assertEqual(clean[0]["notes"], "Annual review. Customer 001 called.")
        self.assertEqual(clean[1]["notes"], "Customer 002 paid; Omer Samples did not")

    def test_the_longest_value_is_replaced_first(self):
        rows = [{"name": "Dana Example", "notes": ""},
                {"name": "Dana Example Junior", "notes": "Dana Example Junior called"}]
        clean, _ = pseudonymise.pseudonymise(rows, ["name", "notes"])
        self.assertEqual(clean[1]["notes"], "Customer 002 called")

    def test_a_value_written_differently_reaches_the_model_as_the_readme_says(self):
        sheet = self.write("sheet.csv", "name,phone,amount_due,days_late,notes\n"
                                        "Invented Customer Alpha,000-000-0001,5,1,call Alpha's brother on 000.000.0001\n")
        request = pipeline.prepare(sheet, PROMPT)["request"]
        self.assertIn("\nCustomer 001,5,1,call Alpha's brother on 000.000.0001\n", request)

    def test_the_command_line_writes_the_clean_sheet_and_the_key_and_refuses_to_overwrite(self):
        out, key = self.base / "clean.csv", self.base / "key.json"
        code, printed, _ = call_main(pseudonymise, [str(SHEET), "--out", str(out), "--key", str(key)])
        self.assertEqual(code, 0)
        self.assertIn("rows 6 | personal values replaced 25", printed)
        clean_text = out.read_text(encoding="utf-8-sig")
        for value in pipeline.personal_values(raw_rows()):
            self.assertNotIn(value, clean_text)
        mapping = json.loads(key.read_text(encoding="utf-8"))
        self.assertEqual(mapping["Customer 003"], "Invented Customer Gamma")
        before = out.read_bytes()
        code, _, err = call_main(pseudonymise, [str(SHEET), "--out", str(out), "--key", str(self.base / "k2.json")])
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(out.read_bytes(), before)


class SheetTest(TempTest):
    """The sheet is checked before anything is sent; a sheet that fails never reaches the model."""

    def refused(self, sheet_text):
        spy = SpyBackend(json.dumps(good_answer()))
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run(self.write("sheet.csv", sheet_text), PROMPT, spy, self.base / "out")
        self.assertEqual((ctx.exception.code, spy.calls), (2, 0))
        self.assertFalse((self.base / "out").exists())
        return str(ctx.exception)

    def test_a_header_spelled_differently_is_refused_before_sending(self):
        text = SHEET.read_text(encoding="utf-8").replace("name,", "Name,", 1)
        self.assertIn("no column 'name'", self.refused(text))

    def test_a_missing_column_is_refused(self):
        self.assertIn("no column 'notes'", self.refused("name,amount_due,days_late\nA B,5,1\n"))

    def test_bad_cells_are_refused_with_the_row_and_the_column(self):
        head = "name,amount_due,days_late,notes\nA B,5,1,\n"
        self.assertIn("row 3 of the sheet: amount_due '4,200'", self.refused(head + 'C D,"4,200",1,\n'))
        self.assertIn("row 3 of the sheet: amount_due '-5'", self.refused(head + "C D,-5,1,\n"))
        self.assertIn("row 3 of the sheet: amount_due ''", self.refused(head + "C D,,1,\n"))
        self.assertIn("row 3 of the sheet: days_late '45.0'", self.refused(head + "C D,5,45.0,\n"))
        self.assertIn("row 3 of the sheet: the name is empty", self.refused(head + ",5,1,\n"))

    def test_missing_files_are_refused(self):
        answer = str(self.answer_file(good_answer()))
        missing = str(self.base / "no-such-file")
        for args, what in [(["--sheet", missing, "--recorded", answer], "the sheet"),
                           (["--prompt", missing, "--recorded", answer], "the prompt"),
                           (["--recorded", missing], "the recorded answer")]:
            code, _, err = call_main(pipeline, args + ["--out", str(self.base / "o")])
            self.assertEqual(code, 2, what)
            self.assertIn(f"REFUSED: cannot read {what}", err)
            self.assertFalse((self.base / "o").exists())

    def test_the_leak_guard_refuses_a_personal_value_before_sending(self):
        # The scrub misses a name broken by a line break; the table joins the line, and the guard catches it.
        text = "name,amount_due,days_late,notes\nInvented Customer Alpha,5,1,\"Ask Invented Customer\nAlpha again\"\n"
        self.assertIn("1 personal value(s) would reach the model", self.refused(text))
        code, _, err = call_main(pipeline, ["--print-request", "--sheet", str(self.base / "sheet.csv")])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)


class GuardTest(unittest.TestCase):
    def setUp(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        self.expected, self.key = prep["expected"], prep["key"]

    def check(self, answer):
        return pipeline.validate(answer, self.expected, self.key)

    def rejected_reasons(self, answer):
        return [r["reason"] for r in self.check(answer)[1]]

    def test_a_correct_answer_is_accepted_whole(self):
        accepted, rejected, missing, notes, rejected_notes = self.check(good_answer())
        self.assertEqual((len(accepted), rejected, missing, rejected_notes), (4, [], [], []))
        self.assertEqual(notes, ["Customer 005 says the invoice never arrived."])

    def test_a_wrong_total_is_rejected(self):
        a = good_answer()
        a["call_list"][2]["amount"] = 4200  # only one of the two invoices
        accepted, rejected, *_ = self.check(a)
        self.assertEqual(len(accepted), 3)
        self.assertIn("the amount 4200 does not match the sheet's total 5700", rejected[0]["reason"])

    def test_wrong_days_late_and_a_bad_priority_are_rejected(self):
        a = good_answer()
        a["call_list"][0]["days_late"] = 91
        self.assertEqual(self.rejected_reasons(a), ["days late 91 does not match the sheet's 90"])
        a = good_answer()
        a["call_list"][1]["priority"] = 1
        self.assertEqual(self.rejected_reasons(a), ["the priority is repeated or below 1"])
        a = good_answer()
        a["call_list"][0]["priority"] = 0
        self.assertEqual(self.rejected_reasons(a), ["the priority is repeated or below 1"])

    def test_the_same_customer_twice_is_rejected(self):
        a = good_answer()
        a["call_list"].append(dict(a["call_list"][3], priority=5))
        self.assertEqual(self.rejected_reasons(a), ["the same customer appears twice"])

    def test_an_item_that_is_not_an_object_is_rejected(self):
        a = good_answer()
        a["call_list"].append(42)
        _, rejected, *_ = self.check(a)
        self.assertEqual(rejected, [{"item": "42", "reason": "not an object"}])

    def test_an_invented_customer_and_a_paid_one_are_rejected(self):
        a = good_answer()
        a["call_list"].append({"customer": "Customer 009", "priority": 5, "amount": 100, "days_late": 1, "reminder": ""})
        a["call_list"].append({"customer": "Customer 004", "priority": 6, "amount": 0, "days_late": 0, "reminder": ""})
        self.assertEqual(sorted(self.rejected_reasons(a)),
                         ["the sheet shows nothing owed", "this customer is not in the sheet"])

    def test_figures_of_the_wrong_kind_are_rejected(self):
        item = ('{"customer": "Customer 002", "priority": 4, "amount": %s, "days_late": %s, "reason": "small", '
                '"reminder": "Hello Customer 002, the balance is 950."}')
        cases = [(('"950"', "12"), "the amount is missing or not a finite number"),
                 (("true", "12"), "the amount is missing or not a finite number"),
                 (("950", "12.0"), "days_late is missing or not a whole number"),
                 (("950", "90.9"), "days_late is missing or not a whole number"),
                 (("950", "true"), "days_late is missing or not a whole number")]
        for values, reason in cases:
            answer = pipeline.parse_answer('{"call_list": [' + item % values + "]}")
            self.assertEqual(self.rejected_reasons(answer), [reason], values)
        for priority in ("4.0", "true"):
            answer = pipeline.parse_answer('{"call_list": [' + (item % ("950", "12")).replace(
                '"priority": 4', '"priority": ' + priority) + "]}")
            self.assertEqual(self.rejected_reasons(answer), ["priority is missing or not a whole number"])
        a = good_answer()
        a["call_list"][3]["amount"] = Decimal("NaN")
        self.assertEqual(self.rejected_reasons(a), ["the amount is missing or not a finite number"])

    def test_nan_and_infinity_refuse_the_whole_answer(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.parse_answer('{"call_list": [{"amount": %s}]}' % constant)
            self.assertEqual(ctx.exception.code, 3)

    def test_a_figure_added_to_a_reminder_is_rejected_with_what_it_is(self):
        a = good_answer()
        a["call_list"][3]["reminder"] = "Hello Customer 002, the balance is 950. Please pay by the 30th of the month."
        self.assertEqual(self.rejected_reasons(a),
                         ["the reminder states a figure other than the total and the days late: 30"])

    def test_a_reminder_must_be_a_text_and_state_the_total(self):
        for reminder in ("", "   ", None, 950):
            a = good_answer()
            a["call_list"][3]["reminder"] = reminder
            self.assertEqual(self.rejected_reasons(a), ["the reminder is missing or empty"], repr(reminder))
        a = good_answer()
        a["call_list"][3]["reminder"] = "Hello Customer 002, your balance is 12 days late."
        self.assertEqual(self.rejected_reasons(a), ["the reminder does not state the total 950"])

    def test_a_reminder_may_name_only_its_own_customer(self):
        cases = [("Hello Customer 002, the balance is 950, like Customer 001.",
                  "the reminder names a stand-in other than this customer: Customer 001"),
                 ("Hello Customer 002, the balance is 950. We tried Phone 002.",
                  "the reminder names a stand-in other than this customer: Phone 002"),
                 ("Hello Customer 002, the balance is 950. ID 777",
                  "the reminder holds an unknown stand-in: ID 777"),
                 ("Hello Customer 002, the balance is 950. Phone 100",
                  "the reminder holds an unknown stand-in: Phone 100")]
        for reminder, reason in cases:
            a = good_answer()
            a["call_list"][3]["reminder"] = reminder
            self.assertEqual(self.rejected_reasons(a), [reason])

    def test_a_reason_may_state_only_the_customers_own_figures_and_stand_ins(self):
        a = good_answer()
        a["call_list"][2]["reason"] = "two invoices, 4200 and 1500, the older 45 days late"
        a["call_list"][0]["reason"] = "Customer 003 did not answer on Phone 003"
        self.assertEqual(self.rejected_reasons(a), [])
        cases = [("pays within 30 days", "the reason states a figure that is not one of this customer's: 30"),
                 ("owes less than the 12800 of the first", "the reason states a figure that is not one of this customer's: 12800"),
                 ("like Customer 002", "the reason names a stand-in from outside this customer's rows: Customer 002"),
                 ("see ID 777", "the reason holds an unknown stand-in: ID 777"),
                 (None, "the reason is missing or not a text")]
        for reason, why in cases:
            a = good_answer()
            a["call_list"][2]["reason"] = reason
            self.assertEqual(self.rejected_reasons(a), [why], reason)

    def test_notes_for_a_human_are_checked_one_by_one(self):
        a = good_answer()
        a["for_a_human"] = ["Customer 001 has a second invoice 20 days late; Phone 003 did not answer.",
                            "Customer 002 promised 300 by Friday.", 42, "ID 777 called back."]
        _, rejected, _, notes, rejected_notes = self.check(a)
        self.assertEqual(rejected, [])
        self.assertEqual(notes, ["Customer 001 has a second invoice 20 days late; Phone 003 did not answer."])
        self.assertEqual([r["reason"] for r in rejected_notes],
                         ["the note states a figure that is not in the sheet: 300", "the note is not a text",
                          "the note holds an unknown stand-in: ID 777"])

    def test_a_customer_left_out_is_listed_as_missing(self):
        a = good_answer()
        del a["call_list"][3]
        self.assertEqual(self.check(a)[2], ["Customer 002"])

    def test_a_fenced_answer_is_read_and_anything_else_is_refused(self):
        text = "```json\n" + json.dumps(good_answer(), ensure_ascii=False) + "\n```"
        self.assertEqual(len(pipeline.parse_answer(text)["call_list"]), 4)
        for bad in ["Here is the list you asked for: ...", '{"for_a_human": []}', '{"call_list": {}}',
                    '{"call_list": [], "for_a_human": "Customer 001 is late"}', "[1, 2]"]:
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.parse_answer(bad)
            self.assertEqual(ctx.exception.code, 3, bad)

    def test_more_than_999_customers_keep_exact_stand_ins(self):
        rows = [{"name": f"Generated Person {i:04d}", "amount_due": "5", "days_late": "2", "notes": ""}
                for i in range(1, 1002)]
        clean, key = pseudonymise.pseudonymise(rows, ["name", "amount_due", "days_late", "notes"])
        self.assertEqual(clean[-1]["name"], "Customer 1001")
        item = {"customer": "Customer 1001", "priority": 1, "amount": 5, "days_late": 2, "reason": "the last one",
                "reminder": "Hello Customer 1001, the balance of 5 is 2 days late."}
        expected = pipeline.expected_by_customer(clean, key)
        accepted, rejected, *_ = pipeline.validate({"call_list": [item]}, expected, key)
        self.assertEqual((len(accepted), rejected), (1, []))
        item["reminder"] += " So is Customer 1000."
        _, rejected, *_ = pipeline.validate({"call_list": [item]}, expected, key)
        self.assertEqual([r["reason"] for r in rejected],
                         ["the reminder names a stand-in other than this customer: Customer 1000"])
        self.assertEqual(pseudonymise.restore("Customer 1001, not Customer 100", key),
                         "Generated Person 1001, not Generated Person 0100")


class MoneyTest(TempTest):
    SHEET_TEXT = ("name,amount_due,days_late,notes\nSmall Debtor,0.1,3,\nSmall Debtor,0.2,5,\n"
                  "Mid Debtor,12345.67,10,\nBig Debtor,1234567.89,30,\n")
    ANSWER = ('{"call_list": ['
              '{"customer": "Customer 003", "priority": 1, "amount": 1234567.89, "days_late": 30, "reason": "largest",'
              ' "reminder": "Hello Customer 003, the balance is 1,234,567.89, 30 days late."},'
              '{"customer": "Customer 002", "priority": 2, "amount": 12345.670, "days_late": 10, "reason": "middle",'
              ' "reminder": "Hello Customer 002, the balance is 12345.67."},'
              '{"customer": "Customer 001", "priority": 3, "amount": 0.3, "days_late": 5,'
              ' "reason": "two small invoices of 0.1 and 0.2", "reminder": "Hello Customer 001, the balance is 0.3."}'
              '], "for_a_human": []}')

    def test_amounts_are_written_in_plain_notation(self):
        for value, text in [("12800", "12800"), ("12800.00", "12800"), ("1.28E+4", "12800"), ("12345.67", "12345.67"),
                            ("1234567.89", "1234567.89"), ("0.30", "0.3"), ("0", "0")]:
            self.assertEqual(pipeline.money(Decimal(value)), text)

    def test_amounts_are_exact_decimals_from_the_sheet_to_the_call_list(self):
        out = self.base / "out"
        sheet = self.write("sheet.csv", self.SHEET_TEXT)
        report = pipeline.run(sheet, PROMPT, pipeline.RecordedBackend(self.write("a.json", self.ANSWER)), out)
        self.assertEqual((report["accepted"], report["rejected"]), (3, []))
        lines = (out / "call-list.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[1:], ["1,Big Debtor,1234567.89,30,largest", "2,Mid Debtor,12345.67,10,middle",
                                     "3,Small Debtor,0.3,5,two small invoices of 0.1 and 0.2"])

    def test_a_rejection_quotes_amounts_exactly(self):
        prep = pipeline.prepare(self.write("sheet.csv", self.SHEET_TEXT), PROMPT)
        answer = pipeline.parse_answer(self.ANSWER.replace("1234567.89, \"days", "1234567.8, \"days"))
        _, rejected, *_ = pipeline.validate(answer, prep["expected"], prep["key"])
        self.assertEqual([r["reason"] for r in rejected],
                         ["the amount 1234567.8 does not match the sheet's total 1234567.89"])


class RunTest(TempTest):
    def test_end_to_end_restores_names_only_locally(self):
        out = self.base / "out"
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(self.answer_file(good_answer())), out)
        self.assertEqual((report["accepted"], report["sent_to_customers"], report["personal_values_in_request"]),
                         (4, 0, 0))
        self.assertEqual((report["for_a_human"], report["rejected_notes"]),
                         (["Customer 005 says the invoice never arrived."], []))
        call_list = (out / "call-list.csv").read_text(encoding="utf-8-sig")
        self.assertIn("Invented Customer Gamma", call_list)
        self.assertNotIn("Customer 003", call_list)  # the stand-in is mapped back in the name and in the reason
        self.assertIn("Invented Customer Gamma", (out / "reminders" / "reminder-01.txt").read_text(encoding="utf-8"))
        self.assertIn("- Invented Customer Epsilon says the invoice never arrived.",
                      (out / "for-a-person.txt").read_text(encoding="utf-8"))
        report_text = (out / "run-report.json").read_text(encoding="utf-8")
        for value in pipeline.personal_values(raw_rows()):
            self.assertNotIn(value, report_text)

    def test_the_page_for_a_person_names_who_was_rejected_or_left_out(self):
        a = good_answer()
        a["call_list"][2]["amount"] = 4200  # Alpha: rejected
        del a["call_list"][3]               # Beta: missing
        a["for_a_human"].append("Customer 003 promised 500 next week.")
        out = self.base / "out"
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(self.answer_file(a)), out)
        page = (out / "for-a-person.txt").read_text(encoding="utf-8")
        self.assertIn("- Invented Customer Alpha: the amount 4200 does not match the sheet's total 5700", page)
        self.assertIn("- Invented Customer Beta\n", page)
        self.assertIn("- Invented Customer Gamma promised 500 next week. (the note states a figure that is not in "
                      "the sheet: 500)", page)
        self.assertEqual(len(report["rejected_notes"]), 1)
        report_text = (out / "run-report.json").read_text(encoding="utf-8")
        for value in pipeline.personal_values(raw_rows()):
            self.assertNotIn(value, report_text)

    def test_the_recorded_model_answer_runs_through_the_guard(self):
        out = self.base / "rec"
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(RECORDED), out)
        # The recorded model answer: all four owing customers, every figure right, the two invoices summed.
        self.assertEqual((report["customers_owing"], report["accepted"]), (4, 4))
        self.assertEqual((report["rejected"], report["missing_from_answer"]), ([], []))
        first = (out / "call-list.csv").read_text(encoding="utf-8-sig").splitlines()[1]
        self.assertTrue(first.startswith("1,Invented Customer Gamma,12800,90,"))

    def test_an_unusable_answer_writes_nothing(self):
        bad = self.write("bad.json", "not json")
        code, _, err = call_main(pipeline, ["--recorded", str(bad), "--out", str(self.base / "o")])
        self.assertEqual(code, 3)
        self.assertIn("REFUSED: the answer is not JSON", err)
        self.assertFalse((self.base / "o").exists())

    def test_an_existing_folder_is_never_overwritten(self):
        (self.base / "o").mkdir()
        code, _, err = call_main(pipeline, ["--recorded", str(self.answer_file(good_answer())),
                                            "--out", str(self.base / "o")])
        self.assertEqual(code, 2)
        self.assertIn("existing folder", err)

    def test_the_api_path_against_a_local_mock(self):
        server = MockServer(json.dumps(good_answer(), ensure_ascii=False))
        try:
            with api_key(TEST_KEY):
                report = pipeline.run(SHEET, PROMPT, pipeline.AnthropicBackend("claude-haiku-5-5", server.url),
                                      self.base / "api")
        finally:
            server.close()
        self.assertEqual(report["accepted"], 4)
        self.assertEqual(len(server.httpd.seen), 1)
        seen = server.httpd.seen[0]
        headers = {k.lower(): v for k, v in seen["headers"].items()}
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers.get("x-api-key"), TEST_KEY)
        body = json.loads(seen["body"])
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-haiku-5-5", 4096))
        self.assertEqual(body["messages"][0]["content"], pipeline.prepare(SHEET, PROMPT)["request"])
        for value in pipeline.personal_values(raw_rows()):
            self.assertNotIn(value, seen["body"])

    def test_api_failures_are_refused_and_write_nothing(self):
        good = json.dumps(good_answer())
        cases = [(dict(reply_text=good, status=500), "HTTP status 500"),
                 (dict(raw=b"<html>not json</html>"), "not JSON"),
                 (dict(raw=b'{"error": "no content"}'), "not in the shape"),
                 (dict(reply_text=good, stop_reason="max_tokens"), "cut off at max_tokens (4096)")]
        for number, (settings, message) in enumerate(cases):
            server = MockServer(**settings)
            out = self.base / f"api-{number}"
            try:
                with api_key(TEST_KEY):
                    code, _, err = call_main(pipeline, ["--backend", "anthropic", "--api-base", server.url,
                                                        "--out", str(out)])
            finally:
                server.close()
            self.assertEqual(code, 3, message)
            self.assertIn(message, err)
            self.assertFalse(out.exists())
        with api_key(TEST_KEY):
            code, _, err = call_main(pipeline, ["--backend", "anthropic", "--api-base", closed_port_url(),
                                                "--out", str(self.base / "api-closed")])
        self.assertEqual(code, 3)
        self.assertIn("the API could not be reached", err)
        self.assertFalse((self.base / "api-closed").exists())

    def test_no_key_means_a_clear_refusal_and_nothing_sent(self):
        old = os.environ.pop(pipeline.API_KEY_ENV, None)
        try:
            code, _, err = call_main(pipeline, ["--backend", "anthropic", "--api-base", "http://127.0.0.1:9",
                                                "--out", str(self.base / "k")])
        finally:
            if old is not None:
                os.environ[pipeline.API_KEY_ENV] = old
        self.assertEqual(code, 2)
        self.assertIn("no API key", err)
        self.assertFalse((self.base / "k").exists())

    def test_the_webhook_gets_stand_ins_only_and_remote_is_refused(self):
        server = MockServer("")
        try:
            report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(self.answer_file(good_answer())),
                                  self.base / "w", webhook=server.url + "/webhook/collections")
        finally:
            server.close()
        self.assertEqual(report["webhook_status"], 200)
        posted = json.loads(server.httpd.seen[0]["body"])
        self.assertEqual(posted["accepted"][0], {"customer": "Customer 003", "priority": 1, "amount": "12800"})
        rows = raw_rows()
        for value in pipeline.personal_values(rows):
            self.assertNotIn(value, server.httpd.seen[0]["body"])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.post_webhook("https://hooks.example.com/x", {}, allow_remote=False)
        spy = SpyBackend(json.dumps(good_answer()))
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run(SHEET, PROMPT, spy, self.base / "remote", webhook="https://hooks.example.com/x")
        self.assertEqual((ctx.exception.code, spy.calls), (2, 0))
        self.assertFalse((self.base / "remote").exists())

    def test_a_failed_webhook_keeps_the_outputs_and_exits_with_4(self):
        server = MockServer("", status=500)
        out = self.base / "wf"
        try:
            code, printed, _ = call_main(pipeline, ["--recorded", str(self.answer_file(good_answer())),
                                                    "--out", str(out), "--webhook", server.url + "/hook"])
        finally:
            server.close()
        self.assertEqual(code, 4)
        self.assertIn("| webhook failed: HTTP 500", printed)
        report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual((report["webhook_status"], report["accepted"]), ("failed: HTTP 500", 4))
        self.assertTrue((out / "call-list.csv").is_file())
        self.assertTrue((out / "for-a-person.txt").is_file())
        code, printed, _ = call_main(pipeline, ["--recorded", str(self.answer_file(good_answer())),
                                                "--out", str(self.base / "wc"), "--webhook", closed_port_url()])
        self.assertEqual(code, 4)
        self.assertIn("| webhook failed: ", printed)
        self.assertTrue((self.base / "wc" / "call-list.csv").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=1)
