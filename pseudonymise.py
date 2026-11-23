"""Replace personal details in a table with stable stand-ins before any AI tool sees it.

The point: a model does not need the names, the identity numbers or the phone numbers to do the work. The answers
are mapped back to the real names locally, after the AI step.

What it does, and no more: the personal columns are replaced whole. In every other column it replaces the sheet's own
personal values (as whole words, in any letter case) and three common patterns: a phone number, an e-mail address
and a nine-digit number. A name or a number written differently inside a note is not recognised.

Standard library only. Reads a CSV, writes a pseudonymised CSV and a local key file. The key file stays on the
user's computer; it is what turns "Customer 007" back into a person after the AI step.
"""
import argparse
import csv
import json
import pathlib
import re
import sys

# Column names (as they appear in the sheet) and the stand-in each gets.
PERSONAL = {
    "name": "Customer",
    "phone": "Phone",
    "email": "Email",
    "id_number": "ID",
    "address": "Address",
}
# Free-text columns are scanned for values that leaked out of their own column.
PHONE = re.compile(r"\b0\d{1,2}[- ]?\d{3}[- ]?\d{4}\b")
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IDNUM = re.compile(r"\b\d{9}\b")


def whole_word(text):
    """A pattern for the text as a whole word in any letter case: "Ann Lee" matches "ANN LEE", never "Annual"."""
    return re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)", re.IGNORECASE)


def pseudonymise(rows, columns):
    """Return (clean rows, key). The same value always gets the same stand-in, so counts and joins still work."""
    key = {}        # stand-in -> original
    reverse = {}    # (column, original) -> stand-in
    counters = {}
    clean = []
    for row in rows:
        out = dict(row)
        for col, label in PERSONAL.items():
            if col not in columns:
                continue
            value = (row.get(col) or "").strip()
            if not value:
                continue
            if (col, value) not in reverse:
                counters[col] = counters.get(col, 0) + 1
                stand_in = f"{label} {counters[col]:03d}"
                reverse[(col, value)] = stand_in
                key[stand_in] = value
            out[col] = reverse[(col, value)]
        clean.append(out)
    # Second pass: personal values quoted inside other columns (a note that repeats a name or a number), longest
    # value first, so a longer value is never cut by a shorter one inside it.
    known = sorted(((orig, stand) for (col, orig), stand in reverse.items()), key=lambda p: -len(p[0]))
    patterns = [(whole_word(orig), stand) for orig, stand in known]
    for out in clean:
        for col in columns:
            if col in PERSONAL:
                continue
            text = out.get(col) or ""
            for pattern, stand in patterns:
                text = pattern.sub(stand, text)
            text = PHONE.sub("[phone removed]", text)
            text = MAIL.sub("[e-mail removed]", text)
            text = IDNUM.sub("[ID removed]", text)
            out[col] = text
    return clean, key


def restore(text, key):
    """Map stand-ins in an AI answer back to the real values. Only exact stand-ins count: "Customer 100" is not
    read inside "Customer 1001"."""
    if not key:
        return text
    tokens = sorted(key, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in tokens) + r")(?!\w)")
    return pattern.sub(lambda m: key[m.group(0)], text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, required=True, help="the pseudonymised CSV to write")
    ap.add_argument("--key", type=pathlib.Path, required=True, help="the local key file to write (never shared)")
    args = ap.parse_args(argv)
    if args.out.exists() or args.key.exists():
        print("refusing to overwrite an existing output or key file", file=sys.stderr)
        return 2
    with args.source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = list(reader)
    clean, key = pseudonymise(rows, columns)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(clean)
    args.key.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rows {len(clean)} | personal values replaced {len(key)} | columns kept {len(columns)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
