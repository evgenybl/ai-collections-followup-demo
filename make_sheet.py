"""Write the synthetic customer sheet: invented businesses that owe money, generated from a seed.

The same seed and count give the same file, byte for byte, on every computer and every Python version. The numbers
come from sha256 over the seed and a counter, not from the random module: Python promises to keep only random()
itself the same across versions, not choice(), shuffle() or randrange(). The shipped customers.synthetic.csv is this
script's output for the committed seed and count, and a test checks that.

What a sheet holds: every customer is an invented business whose name ends in "(invented)"; identity numbers and
phone numbers are obvious placeholders; e-mail addresses are at example.com. Most customers owe on one, two or three
open invoices, and a few have paid in full. The delays reach every ageing bucket, and some amounts have cents. The
notes are of the kinds a real sheet carries: a promise to pay, an instalment request, an unanswered phone, a disputed
invoice, an identity note that repeats a personal value, and a later invoice that names the customer again.

Standard library only. Run: python -I make_sheet.py --out <file>  (the committed seed and count by default)
Exit codes: 0 written; 2 refused (an existing file, a count out of range).
"""
import argparse
import csv
import hashlib
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from spreadsheet import safe_cells  # noqa: E402

DEFAULT_SEED = 7
DEFAULT_CUSTOMERS = 25
COLUMNS = ["name", "id_number", "phone", "email", "address", "amount_due", "days_late", "notes"]

FIRST_WORDS = ["Harbour", "Maple", "Copper", "Lantern", "Willow", "Granite", "Juniper", "Meadow", "Riverbend",
               "Saffron", "Thistle", "Cobalt", "Bramble", "Orchard", "Kestrel", "Linden", "Marigold", "Pebble",
               "Quarry", "Sorrel"]
TRADES = ["Bakery", "Joinery", "Print Shop", "Garden Supplies", "Bicycle Repairs", "Tile Works", "Catering",
          "Laundry", "Bookbinding", "Upholstery", "Glass Studio", "Pottery", "Locksmiths", "Florist", "Framing"]
MIN_CUSTOMERS = 5                                  # one owing customer for each ageing bucket at least
MAX_CUSTOMERS = len(FIRST_WORDS) * len(TRADES)     # every customer gets a name of their own

# Open invoices per customer, repeated over the customers before they are shuffled: in every eight, three owe on one
# invoice, three on two, one on three, and one has paid in full (0).
INVOICES = [1, 2, 1, 3, 2, 1, 0, 2]
# The bucket of each owing customer's oldest invoice, repeated the same way: more recent debts than old ones.
BUCKETS = ["1-30", "31-60", "61-90", "over 90", "1-30", "current", "31-60"]
BUCKET_DAYS = {"current": (0, 0), "1-30": (1, 30), "31-60": (31, 60), "61-90": (61, 90), "over 90": (91, 180)}
# The note on an owing customer's first row, repeated the same way.
NOTE_KINDS = ["promise", "instalments", "no-answer", "never-arrived", "identity", "none", "not-finished", "none"]
LATER = {1: "second", 2: "third"}
CENTS = [15, 25, 35, 45, 55, 65, 75, 85, 95]


class Draws:
    """A deterministic stream of whole numbers: each is read from sha256 of "<seed>:<counter>"."""

    def __init__(self, seed):
        self.seed, self.counter = seed, 0

    def below(self, n):
        """A whole number from 0 to n - 1."""
        self.counter += 1
        digest = hashlib.sha256(f"{self.seed}:{self.counter}".encode("ascii")).digest()
        return int.from_bytes(digest[:8], "big") % n

    def between(self, low, high):
        """A whole number from low to high, both included."""
        return low + self.below(high - low + 1)

    def shuffled(self, items):
        """A copy of the list in a deterministic order (Fisher-Yates)."""
        items = list(items)
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]
        return items


def repeated(pattern, count):
    return [pattern[i % len(pattern)] for i in range(count)]


def slug(name):
    """harbour-print-shop for "Harbour Print Shop (invented)"."""
    return "-".join(name.replace("(invented)", "").lower().split())


def amount(draws):
    """One open invoice: 150 to 15000 in steps of 5; about one in three has cents."""
    whole = draws.between(30, 3000) * 5
    if draws.below(3) == 0:
        return f"{whole}.{CENTS[draws.below(len(CENTS))]}"
    return str(whole)


def first_note(kind, person):
    if kind == "promise":
        return "Promised to pay by the end of the month"
    if kind == "instalments":
        return "Asked to split the payment into two instalments"
    if kind == "no-answer":
        return f"No answer on {person['phone']}. Try {person['email']} instead"
    if kind == "never-arrived":
        return "Says the invoice never arrived"
    if kind == "not-finished":
        return "Disputes the invoice: says the work was not finished"
    if kind == "identity":
        return f"Identity verified against {person['id_number']}"
    return ""


def business_names(draws, count):
    """Distinct invented names. Customer i = 20q + r takes first word r and trade r + q (modulo 15) of two shuffled
    lists, so a first word comes back only after all twenty were used, and no name repeats up to MAX_CUSTOMERS."""
    firsts, trades = draws.shuffled(FIRST_WORDS), draws.shuffled(TRADES)
    names = []
    for i in range(count):
        q, r = divmod(i, len(firsts))
        names.append(f"{firsts[r]} {trades[(r + q) % len(trades)]} (invented)")
    return names


def sheet_rows(seed, customers):
    """The rows of the sheet as dicts over COLUMNS, in their order in the file. A customer's first row is their
    oldest invoice; a later invoice lands further down the sheet, as it would in a sheet kept by hand."""
    draws = Draws(seed)
    names = business_names(draws, customers)
    invoices = draws.shuffled(repeated(INVOICES, customers))
    owing = [i for i, count in enumerate(invoices) if count > 0]
    buckets = dict(zip(owing, draws.shuffled(repeated(BUCKETS, len(owing)))))
    notes = dict(zip(owing, draws.shuffled(repeated(NOTE_KINDS, len(owing)))))
    placed = []  # (place in the file, row)
    for i, name in enumerate(names):
        n = i + 1
        person = {"name": name, "id_number": f"{n:09d}", "phone": f"000-000-{n:04d}",
                  "email": slug(name) + "@example.com", "address": f"{n} Example Street Example City"}
        if invoices[i] == 0:
            placed.append(((n, 0, len(placed)), dict(person, amount_due="0", days_late="0", notes="Paid in full")))
            continue
        low, high = BUCKET_DAYS[buckets[i]]
        oldest = draws.between(low, high)
        place = n
        for later in range(invoices[i]):
            if later == 0:
                days, note = oldest, first_note(notes[i], person)
            else:
                place += draws.between(1, 6)
                days = draws.between(0, oldest)
                note = f"A {LATER[later]} invoice for {name}" if draws.below(2) == 0 else ""
            row = dict(person, amount_due=amount(draws), days_late=str(days), notes=note)
            placed.append(((place, later, len(placed)), row))
    return [row for _, row in sorted(placed, key=lambda p: p[0])]


def sheet_text(rows):
    """The CSV text: the header and the rows, LF line ends, quoted only where a cell needs it, and a cell a
    spreadsheet could run as a formula written with a leading apostrophe (spreadsheet.py)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(safe_cells(row[column] for column in COLUMNS)[0])
    return buffer.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS,
                    help=f"from {MIN_CUSTOMERS} to {MAX_CUSTOMERS}")
    ap.add_argument("--out", type=pathlib.Path, required=True, help="the CSV to write (a file that does not exist yet)")
    args = ap.parse_args(argv)
    if not MIN_CUSTOMERS <= args.customers <= MAX_CUSTOMERS:
        print(f"REFUSED: --customers must be from {MIN_CUSTOMERS} to {MAX_CUSTOMERS}", file=sys.stderr)
        return 2
    if args.out.exists():
        print(f"REFUSED: refusing to overwrite an existing file: {args.out}", file=sys.stderr)
        return 2
    rows = sheet_rows(args.seed, args.customers)
    args.out.write_bytes(sheet_text(rows).encode("utf-8"))
    paid = len({r["name"] for r in rows if r["notes"] == "Paid in full"})
    print(f"rows {len(rows)} | customers {args.customers} | owing {args.customers - paid} | paid in full {paid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
