"""Tests of the generated sheet (make_sheet.py), the ageing buckets, and the shipped recorded answer.
Run: python -I test_sheet.py  (every test file: python -I run_tests.py)"""
import csv
import hashlib
import json
import pathlib
import sys
import unittest
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_sheet  # noqa: E402
import pipeline  # noqa: E402
from testsupport import (META, PROMPT, RECORDED, SHEET, TempTest, call_main, constructed_answer,  # noqa: E402
                         constructed_answer_text)

# The sha256 of recorded-answer.json as the model returned it, so that an edited recording is caught.
ANSWER_SHA256 = "253d62887849e1c3905fa8ab6384b7216ca09e603786956c622c8deae4b35757"


def customers(rows):
    """name -> the customer's rows."""
    out = {}
    for row in rows:
        out.setdefault(row["name"], []).append(row)
    return out


class SheetGeneratorTest(TempTest):
    def test_the_shipped_sheet_is_the_generators_output_for_the_committed_seed(self):
        out = self.base / "sheet.csv"
        code, printed, _ = call_main(make_sheet, ["--seed", str(make_sheet.DEFAULT_SEED),
                                                  "--customers", str(make_sheet.DEFAULT_CUSTOMERS), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(out.read_bytes(), SHEET.read_bytes(), "the shipped sheet is not the generator's output")
        self.assertIn(f"| customers {make_sheet.DEFAULT_CUSTOMERS} |", printed)

    def test_the_same_seed_gives_the_same_sheet_and_another_seed_another(self):
        texts = []
        for name, seed in [("a.csv", 11), ("b.csv", 11), ("c.csv", 12)]:
            code, _, _ = call_main(make_sheet, ["--seed", str(seed), "--customers", "30", "--out",
                                                str(self.base / name)])
            self.assertEqual(code, 0)
            texts.append((self.base / name).read_bytes())
        self.assertEqual(texts[0], texts[1])
        self.assertNotEqual(texts[0], texts[2])

    def test_an_existing_file_and_a_count_out_of_range_are_refused(self):
        existing = self.write("there.csv", "keep me")
        code, _, err = call_main(make_sheet, ["--out", str(existing)])
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")
        for count in (str(make_sheet.MIN_CUSTOMERS - 1), str(make_sheet.MAX_CUSTOMERS + 1)):
            code, _, err = call_main(make_sheet, ["--customers", count, "--out", str(self.base / "new.csv")])
            self.assertEqual(code, 2, count)
            self.assertIn("--customers must be from", err)
            self.assertFalse((self.base / "new.csv").exists())

    def test_the_largest_count_still_gives_every_customer_a_name_of_their_own(self):
        rows = make_sheet.sheet_rows(3, make_sheet.MAX_CUSTOMERS)
        self.assertEqual(len(customers(rows)), make_sheet.MAX_CUSTOMERS)

    def test_the_shipped_sheet_has_what_the_specification_asks(self):
        rows, _ = pipeline.load_sheet(SHEET)   # it passes the pipeline's own checks
        by_name = customers(rows)
        open_counts = [len([r for r in rs if Decimal(r["amount_due"]) > 0]) for rs in by_name.values()]
        self.assertIn(2, open_counts)
        self.assertIn(3, open_counts)
        self.assertGreaterEqual(open_counts.count(0), 2, "a few customers have paid in full")
        expected = pipeline.prepare(SHEET, PROMPT)["expected"]
        buckets = {pipeline.bucket_of(c) for c in expected.values() if c["total"] > 0}
        self.assertEqual(buckets, set(pipeline.BUCKETS))
        amounts = [r["amount_due"] for r in rows if Decimal(r["amount_due"]) > 0]
        self.assertTrue(any("." in a for a in amounts) and any("." not in a for a in amounts))
        notes = " ".join(r["notes"] for r in rows)
        for kind in ("Promised to pay", "instalments", "No answer on", "never arrived", "Disputes the invoice",
                     "Identity verified against", "A second invoice for", "Paid in full"):
            self.assertIn(kind, notes)
        for row in rows:
            self.assertTrue(row["name"].endswith(" (invented)"), row["name"])
            self.assertTrue(row["email"].endswith("@example.com"), row["email"])
            self.assertTrue(row["id_number"].startswith("0000000") and row["phone"].startswith("000-000-"))
        self.assertTrue(any(r["id_number"] in r["notes"] for r in rows), "an identity note repeats a personal value")

    def test_the_shipped_request_carries_no_personal_value(self):
        rows, _ = pipeline.load_sheet(SHEET)
        request = pipeline.prepare(SHEET, PROMPT)["request"].lower()
        for value in pipeline.personal_values(rows):
            self.assertNotIn(value.lower(), request)
        self.assertNotIn("(invented)", request)


class AgeingTest(TempTest):
    def test_the_buckets_at_their_edges(self):
        for days, bucket in [(0, "current"), (1, "1-30"), (30, "1-30"), (31, "31-60"), (60, "31-60"),
                             (61, "61-90"), (90, "61-90"), (91, "over 90"), (400, "over 90")]:
            self.assertEqual(pipeline.ageing_bucket(days), bucket, days)

    def test_the_delay_and_the_bucket_come_from_the_rows_that_still_owe_money(self):
        sheet = self.write("sheet.csv", "name,amount_due,days_late,notes\nAlpha Shop,100,10,\nAlpha Shop,0,95,\n"
                                        "Beta Shop,50,0,\nGamma Shop,0,40,\n")
        prep = pipeline.prepare(sheet, PROMPT)
        expected = prep["expected"]
        self.assertEqual(expected["Customer 001"]["days"], 10)        # the paid row's older delay is not the delay
        self.assertEqual(pipeline.bucket_of(expected["Customer 001"]), "1-30")
        item = {"customer": "Customer 001", "priority": 1, "amount": 100, "days_late": 10,
                "reason": "a paid invoice was 95 days late", "reminder": "Dear Customer 001, 100 is 10 days late."}
        accepted, rejected, *_ = pipeline.validate({"call_list": [item]}, expected, prep["key"])
        self.assertEqual((len(accepted), rejected), (1, []))
        _, rejected, *_ = pipeline.validate({"call_list": [dict(item, days_late=95)]}, expected, prep["key"])
        self.assertEqual([r["reason"] for r in rejected], ["days late 95 does not match the sheet's 10"])
        self.assertEqual(pipeline.bucket_of(expected["Customer 002"]), "current")
        ageing = {b["bucket"]: b for b in pipeline.ageing(expected)}
        self.assertEqual(ageing["1-30"], {"bucket": "1-30", "customers": ["Customer 001"], "total": "100"})
        self.assertEqual(ageing["current"]["customers"], ["Customer 002"])
        self.assertEqual([b["bucket"] for b in pipeline.ageing(expected)], list(pipeline.BUCKETS))
        self.assertEqual(sum(len(b["customers"]) for b in ageing.values()), 2)   # Gamma owes nothing

    def test_the_bucket_is_never_sent_to_the_model(self):
        request = pipeline.prepare(SHEET, PROMPT)["request"]
        self.assertNotIn("bucket", request.lower())
        self.assertNotIn("ageing", request.lower())
        self.assertIn("\n\nThe table:\n\nname,amount_due,days_late,notes\n", request)

    def test_the_bucket_is_in_the_call_list_and_the_run_report(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        out = self.base / "run"
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(self.answer_file(constructed_answer(prep))), out)
        with (out / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), ["priority", "customer", "amount", "days_late", "bucket", "reason"])
        self.assertEqual(len(rows), report["customers_owing"])
        for row in rows:
            self.assertEqual(row["bucket"], pipeline.ageing_bucket(int(row["days_late"])), row["customer"])
        self.assertEqual(sum(Decimal(b["total"]) for b in report["ageing"]), Decimal(report["total_owed"]))
        self.assertEqual(Decimal(report["total_owed"]), sum(Decimal(r["amount"]) for r in rows))
        self.assertEqual(sum(len(b["customers"]) for b in report["ageing"]), report["customers_owing"])
        report_text = (out / "run-report.json").read_text(encoding="utf-8")
        self.assertNotIn("(invented)", report_text)   # stand-ins only


class RecordedAnswerTest(TempTest):
    def test_the_recorded_answer_was_saved_for_the_request_built_today(self):
        meta = json.loads(META.read_text(encoding="utf-8"))
        self.assertEqual(sorted(meta), ["answer_sha256", "model", "recorded_on", "request_sha256", "route"])
        request = pipeline.prepare(SHEET, PROMPT)["request"]
        self.assertEqual(meta["request_sha256"], hashlib.sha256(request.encode("utf-8")).hexdigest(),
                         "the prompt or the sheet changed since the answer was recorded: record it again")

    def test_the_answer_file_has_not_changed_since_it_was_saved(self):
        """The sha256 proves only this, and that the file answers the request built today (the test above). That a
        model wrote it is the author's statement, which no file here can prove."""
        meta = json.loads(META.read_text(encoding="utf-8"))
        self.assertEqual(meta["answer_sha256"], ANSWER_SHA256)
        self.assertEqual(hashlib.sha256(RECORDED.read_bytes()).hexdigest(), ANSWER_SHA256,
                         "recorded-answer.json has changed since it was saved")

    def test_the_shipped_recorded_answer_runs_through_the_guard(self):
        out = self.base / "rec"
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(RECORDED), out)
        # What the checks do with the recording: every owing customer accepted, every figure right.
        self.assertEqual((report["customers_owing"], report["accepted"]), (22, 22))
        self.assertEqual((report["rejected"], report["missing_from_answer"], report["rejected_notes"]), ([], [], []))
        first = (out / "call-list.csv").read_text(encoding="utf-8-sig").splitlines()[1]
        self.assertTrue(first.startswith("1,Cobalt Bakery (invented),12295,123,over 90,"), first)

    def test_the_constructed_answer_passes_every_check(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        accepted, rejected, missing, notes, rejected_notes = pipeline.validate(
            pipeline.parse_answer(constructed_answer_text(prep)), prep["expected"], prep["key"])
        owing = [who for who, c in prep["expected"].items() if c["total"] > 0]
        self.assertEqual((len(accepted), rejected, missing, rejected_notes), (len(owing), [], [], []))
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main(verbosity=1)
