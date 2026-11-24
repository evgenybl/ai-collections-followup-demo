"""Tests of the checks added after an independent review: loose references to stand-ins, what a reminder may not
hold, the sheet check's limits, the leak guard's whole words, and the amounts kept exact after the scrub.
Run: python -I test_checks.py  (every test file: python -I run_tests.py)"""
import json
import pathlib
import sys
import unittest
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline  # noqa: E402
from testsupport import PROMPT, SHEET, SpyBackend, TempTest, constructed_answer  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
