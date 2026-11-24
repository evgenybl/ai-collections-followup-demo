"""Tests of the report page: its parts and their order, its figures from the run's own files, escaping, and refusals.
Run: python -I test_report.py  (every test file: python -I run_tests.py)"""
import csv
import datetime
import io
import json
import pathlib
import re
import sys
import unittest
from decimal import Decimal
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
from testsupport import (PROMPT, SHEET, MockServer, TempTest, api_key, call_main,  # noqa: E402
                         constructed_answer)

AS_OF = "2026-11-24"
AT = "2026-11-24T09:00:00Z"
BY = "Reviewer One (invented)"
PARTS = ["header", "figures", "ageing", "call-list", "reminders", "refused", "already-drafted", "notes", "footer"]


class ReportTest(TempTest):
    def run_pipeline(self, answer, name="run", *extra, sheet=SHEET):
        out = self.base / name
        code, _, err = call_main(pipeline, ["--sheet", str(sheet), "--recorded", str(self.answer_file(answer)),
                                            "--out", str(out), "--as-of", AS_OF, *extra])
        self.assertEqual(code, 0, err)
        return out

    def page(self, run, name="report.html"):
        code, printed, err = call_main(report, [str(run), "--out", str(self.base / name)])
        self.assertEqual(code, 0, err)
        self.assertTrue(printed.startswith(f"report written to {(self.base / name).as_posix()} | customers owing "))
        return (self.base / name).read_text(encoding="utf-8")

    def figure(self, html_text, name):
        return re.search(r"<span>" + re.escape(name) + r"</span><b>([^<]*)</b>", html_text).group(1)

    def test_the_page_has_its_nine_parts_in_order(self):
        text = self.page(self.run_pipeline(constructed_answer(pipeline.prepare(SHEET, PROMPT))))
        places = [text.index(f'id="{part}"') for part in PARTS]
        self.assertEqual(places, sorted(places))
        header = text[places[0]:places[1]]
        for words in ("Synthetic data", "Recorded", f"As of {AS_OF}", "This page holds real names"):
            self.assertIn(words, header)
        footer = text[places[-1]:]
        self.assertEqual(footer.count("<p"), 4)  # three lines on what was checked and what was not, then the last
        self.assertIn("Nothing was sent to any customer.", footer)

    def test_the_page_is_self_contained(self):
        text = self.page(self.run_pipeline(constructed_answer(pipeline.prepare(SHEET, PROMPT))))
        for outside in ("<script", "<link", "src=", "href=", "http://", "https://", "@import", "url(", "@font-face"):
            self.assertNotIn(outside, text.lower())
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', text)
        self.assertIn("@media print", text)
        self.assertIn("print-color-adjust: exact", text)  # the bars stay visible in a print preview

    def test_every_figure_comes_from_the_run_files(self):
        run = self.run_pipeline(constructed_answer(pipeline.prepare(SHEET, PROMPT)))
        text = self.page(run)
        data = json.loads((run / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual(self.figure(text, "Customers owing"), str(data["customers_owing"]))
        self.assertEqual(self.figure(text, "Total owed"), report.shown(data["total_owed"]))
        self.assertEqual(self.figure(text, "Drafts waiting for a decision"), str(len(data["drafts"])))
        self.assertEqual(self.figure(text, "Items the checks refused"), "0")
        total = Decimal(data["total_owed"])
        for bucket in data["ageing"]:
            width = Decimal(bucket["total"]) / total * 100
            self.assertIn(f'<span>{bucket["bucket"]}</span><span class="track"><span class="bar" style="width: '
                          f'{width:.1f}%"></span></span><span class="value">{report.shown(bucket["total"])} ', text)
        with (run / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                self.assertIn(f'<td>{row["priority"]}</td><td>{row["customer"]}</td><td class="number">'
                              f'{report.shown(row["amount"])}</td><td class="number">{row["days_late"]}</td>'
                              f'<td>{row["bucket"]}</td>', text)
        reminder = (run / "reminders" / "reminder-01.txt").read_text(encoding="utf-8").rstrip()
        self.assertIn(f"<pre>{reminder}</pre>", text)

    def test_text_from_the_model_or_the_sheet_is_shown_as_text(self):
        sheet = self.write("sheet.csv", 'name,amount_due,days_late,notes\n"Bold <b>Shop</b> & Co (invented)",100,5,\n')
        answer = {"call_list": [{"customer": "Customer 001", "priority": 1, "amount": 100, "days_late": 5,
                                 "reason": "the only one <i>here</i>",
                                 "reminder": "Dear Customer 001, the balance of 100 is 5 days late. <img>"}],
                  "for_a_human": ['<script>alert("x")</script>']}
        text = self.page(self.run_pipeline(answer, sheet=sheet))
        self.assertNotIn("<script", text)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", text)
        self.assertIn("Bold &lt;b&gt;Shop&lt;/b&gt; &amp; Co (invented)", text)
        self.assertNotIn("<b>Shop", text)
        self.assertIn("the only one &lt;i&gt;here&lt;/i&gt;", text)
        self.assertIn("5 days late. &lt;img&gt;</pre>", text)

    def test_the_page_shows_the_decisions_when_it_is_written_again(self):
        run = self.run_pipeline(constructed_answer(pipeline.prepare(SHEET, PROMPT)))
        before = self.page(run)
        drafts = int(self.figure(before, "Drafts waiting for a decision"))
        self.assertNotIn("Approved by", before)
        call_main(approval, ["approve", str(run), "--item", "1", "--by", BY, "--at", AT])
        call_main(approval, ["reject", str(run), "--item", "2", "--by", BY, "--at", AT, "--reason", "<wait>"])
        after = self.page(run)
        self.assertEqual(self.figure(after, "Drafts waiting for a decision"), str(drafts - 2))
        self.assertIn(f'<span class="state approved">Approved by {BY} at {AT}</span>', after)
        self.assertIn(f'<span class="state rejected">Rejected by {BY} at {AT}: &lt;wait&gt;</span>', after)

    def test_refusals_the_history_and_a_changed_amount_are_shown(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        owing = sorted(who for who, c in prep["expected"].items() if c["total"] > 0)
        drafted, changed = owing[0], owing[1]
        line = {"exported_on": "2026-11-20", "request_sha256": "ab" * 32}
        history = self.write("history.jsonl", "".join(json.dumps(dict(
            line, customer_key=prep["customer_keys"][who], total=total)) + "\n" for who, total in [
                (drafted, pipeline.money(prep["everyone"][drafted]["total"])), (changed, "1")]))
        records = pipeline.read_history(history, datetime.date(2026, 11, 24))
        answer = constructed_answer(pipeline.prepare(SHEET, PROMPT, records, datetime.date(2026, 11, 24)))
        wrong = answer["call_list"][-1]
        wrong["amount"] = wrong["amount"] + 100
        answer["for_a_human"].append(f"{changed} owes 7777 more.")
        text = self.page(self.run_pipeline(answer, "run", "--history", str(history)))
        name = prep["key"]
        self.assertEqual(self.figure(text, "Items the checks refused"), "1")
        self.assertIn(f"<li><b>{name[wrong['customer']]}</b>: the amount ", text)
        self.assertIn(f"{name[changed]} owes 7777 more. <i>(the note states a figure that is not in the sheet: "
                      "7777)</i>", text)
        total = report.shown(pipeline.money(prep["everyone"][drafted]["total"]))
        self.assertIn(f"<li>{name[drafted]}: drafted on 2026-11-20, total {total}</li>", text)
        self.assertIn(f"{name[changed]}<span class=\"changed\">amount changed since the last draft</span>", text)
        self.assertIn(f"<li>{name[changed]}: ", text)

    def test_the_label_says_live_only_for_the_default_api_address(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        answer = json.dumps(constructed_answer(prep))
        # 1. A mock on this computer: the live path, but not live.
        server = MockServer(answer)
        try:
            with api_key("unit" + "-" + "test" + "-" + "placeholder"):
                pipeline.run(SHEET, PROMPT, pipeline.AnthropicBackend(api_base=server.url), self.base / "mock")
        finally:
            server.close()
        host = f"127.0.0.1:{server.httpd.server_address[1]}"
        data = json.loads((self.base / "mock" / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual((data["api_host"], data["api_default_address"]), (host, False))
        text = self.page(self.base / "mock", "mock.html")
        self.assertIn(f'<span class="tag">Live path, custom endpoint {host}</span>', text)
        self.assertNotIn('<span class="tag">Live</span>', text)
        # 2. The default address. urlopen is replaced in this process, so the request goes nowhere.
        seen = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

        def fake_urlopen(request, timeout):
            seen.append(request.full_url)
            return Reply(json.dumps({"type": "message", "content": [{"type": "text", "text": answer}],
                                     "stop_reason": "end_turn"}).encode("utf-8"))

        with mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen):
            with api_key("unit" + "-" + "test" + "-" + "placeholder"):
                pipeline.run(SHEET, PROMPT, pipeline.AnthropicBackend(), self.base / "default")
        self.assertEqual(seen, [pipeline.DEFAULT_API_BASE + "/v1/messages"])
        data = json.loads((self.base / "default" / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual((data["api_host"], data["api_default_address"]), ("api.anthropic.com", True))
        self.assertIn('<span class="tag">Live</span>', self.page(self.base / "default", "default.html"))
        # 3. The recorded answer.
        run = self.run_pipeline(constructed_answer(prep))
        data = json.loads((run / "run-report.json").read_text(encoding="utf-8"))
        self.assertEqual((data["api_host"], data["api_default_address"]), (None, None))
        self.assertIn('<span class="tag">Recorded</span>', self.page(run))
        owing = [who for who, c in prep["expected"].items() if c["total"] > 0]
        history = self.write("all.jsonl", "".join(json.dumps({
            "customer_key": prep["customer_keys"][who], "exported_on": AS_OF,
            "total": pipeline.money(prep["everyone"][who]["total"]), "request_sha256": "ab" * 32}) + "\n"
            for who in owing))
        text = self.page(self.run_pipeline({"call_list": []}, "none", "--history", str(history)), "none.html")
        self.assertIn("Recorded: the model was not called, every owing customer was already drafted", text)
        self.assertIn("<p>No drafts in this run.</p>", text)

    def test_a_folder_that_is_not_a_run_or_decisions_from_another_run_are_refused(self):
        empty = self.base / "empty"
        empty.mkdir()
        code, _, err = call_main(report, [str(empty), "--out", str(self.base / "r.html")])
        self.assertEqual(code, 2)
        self.assertIn("not a run folder", err)
        run = self.run_pipeline(constructed_answer(pipeline.prepare(SHEET, PROMPT)))
        call_main(approval, ["approve", str(run), "--item", "1", "--by", BY, "--at", AT])
        data = json.loads((run / "decisions.json").read_text(encoding="utf-8"))
        (run / "decisions.json").write_text(json.dumps(dict(data, request_sha256="0" * 64)), encoding="utf-8")
        code, _, err = call_main(report, [str(run), "--out", str(self.base / "r.html")])
        self.assertEqual(code, 2)
        self.assertIn("decisions.json names another run", err)
        self.assertFalse((self.base / "r.html").exists())
        (run / "decisions.json").unlink()
        reminder = run / "reminders" / "reminder-02.txt"
        reminder.write_text(reminder.read_text(encoding="utf-8") + "P.S. edited by hand\n", encoding="utf-8")
        code, _, err = call_main(report, [str(run), "--out", str(self.base / "r.html")])
        self.assertEqual(code, 2)
        self.assertIn("a draft was changed after it was written", err)
        self.assertFalse((self.base / "r.html").exists())

    def test_amounts_are_shown_for_a_reader_and_never_rounded(self):
        for text, shown in [("12800", "12,800"), ("34256.4", "34,256.40"), ("1234567.89", "1,234,567.89"),
                            ("0.3", "0.30"), ("0", "0"), ("0.333", "0.333"), ("12345.678", "12345.678")]:
            self.assertEqual(report.shown(text), shown, text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
