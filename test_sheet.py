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
from testsupport import (META, PLACEHOLDER_ROUTE, PROMPT, RECORDED, SHEET, TempTest, call_main,  # noqa: E402
                         constructed_answer, constructed_answer_text)


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

    def test_the_bucket_comes_from_the_largest_delay_among_open_rows(self):
        sheet = self.write("sheet.csv", "name,amount_due,days_late,notes\nAlpha Shop,100,10,\nAlpha Shop,0,95,\n"
                                        "Beta Shop,50,0,\nGamma Shop,0,40,\n")
        expected = pipeline.prepare(sheet, PROMPT)["expected"]
        self.assertEqual(expected["Customer 001"]["days"], 95)        # the check of days_late still uses every row
        self.assertEqual(pipeline.bucket_of(expected["Customer 001"]), "1-30")   # the paid row is not open
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
    def test_the_shipped_recorded_answer_meta_file_matches_the_request(self):
        meta = json.loads(META.read_text(encoding="utf-8"))
        self.assertEqual(sorted(meta), ["model", "recorded_on", "request_sha256", "route"])
        request = pipeline.prepare(SHEET, PROMPT)["request"]
        self.assertEqual(meta["request_sha256"], hashlib.sha256(request.encode("utf-8")).hexdigest(),
                         "the prompt or the sheet changed since the answer was recorded: record it again")

    def test_the_shipped_recorded_answer_runs_through_the_guard(self):
        report = pipeline.run(SHEET, PROMPT, pipeline.RecordedBackend(RECORDED), self.base / "rec")
        self.assertEqual(report["accepted"], report["customers_owing"])
        self.assertEqual((report["rejected"], report["missing_from_answer"], report["rejected_notes"]), ([], [], []))

    def test_a_placeholder_recording_is_exactly_the_constructed_answer(self):
        meta = json.loads(META.read_text(encoding="utf-8"))
        if meta["route"] != PLACEHOLDER_ROUTE:
            self.skipTest("recorded-answer.json is a real recording, not the placeholder")
        self.assertEqual(RECORDED.read_text(encoding="utf-8"), constructed_answer_text(pipeline.prepare(SHEET, PROMPT)))
        self.assertIn("not a model's answer", meta["model"])

    def test_the_constructed_answer_passes_every_check(self):
        prep = pipeline.prepare(SHEET, PROMPT)
        accepted, rejected, missing, notes, rejected_notes = pipeline.validate(
            pipeline.parse_answer(constructed_answer_text(prep)), prep["expected"], prep["key"])
        owing = [who for who, c in prep["expected"].items() if c["total"] > 0]
        self.assertEqual((len(accepted), rejected, missing, rejected_notes), (len(owing), [], [], []))
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main(verbosity=1)
