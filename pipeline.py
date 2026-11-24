"""An AI collections follow-up pipeline with a model API call and an automation webhook (synthetic data only).

The steps, in order:
1. The sheet is checked: the four needed columns are there, every row has a name, every amount is a plain decimal
   number and every delay a whole number. A sheet that fails is refused before anything is sent.
2. The personal columns are replaced with stand-ins and the notes are scrubbed (pseudonymise.py). A guard refuses
   the run if a personal value of the sheet is still in the request.
3. Only the columns the task needs (the stand-in name, the debt, the days late, the cleaned notes) go to a model,
   with the fixed prompt in prompt.txt. The backend is a recorded answer, or the Anthropic Messages API.
4. The answer must be JSON in a fixed shape. Every figure it writes in digits must match the sheet, and every
   stand-in in it must belong where it is used. An item that fails is rejected and listed for a person to check; a
   customer the model left out is listed too, and so is a note for a person that fails.
5. The stand-ins are mapped back to the real names on this computer only.
6. The call list, one reminder draft per customer, a page for a person (real names) and a run report (stand-ins
   only) are written. The run summary may be POSTed to an automation webhook (an n8n or Make trigger) on this
   computer. It carries stand-ins only.

Each owing customer's ageing bucket (current, 1-30, 31-60, 61-90, over 90) is computed here from the largest delay
among their open rows. It is never stored in the sheet, and the model is neither asked for it nor told it.

With --history, a run reads the run history that approval.py export writes. A customer exported inside the window
(--window-days before --as-of) with the same total is left out of the request and listed as already drafted; one
whose total changed is drafted again and marked. When every owing customer is already drafted, the model is not
called at all. Without --history, every owing customer is asked about, as before.

There is no step that sends anything to a customer. Standard library only. Run: python -I pipeline.py --help
Exit codes: 0 done; 2 refused (usage, a missing file, the sheet, a corrupt history line, a personal value in the
request); 3 refused (the model's answer or the API cannot be used; nothing is written); 4 the outputs were written
but the webhook failed.
"""
import argparse
import csv
import datetime
import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pseudonymise  # noqa: E402
from spreadsheet import FORMULA_START, safe_cells  # noqa: E402,F401

NAME, AMOUNT, DAYS, NOTES = "name", "amount_due", "days_late", "notes"
ID_NUMBER = "id_number"
MODEL_COLUMNS = [NAME, AMOUNT, DAYS, NOTES]
KNOWN_COLUMNS = MODEL_COLUMNS + [c for c in pseudonymise.PERSONAL if c not in MODEL_COLUMNS]
MAX_WHOLE_DIGITS = 15  # an amount's digits before the point; a longer one is refused, so that no total is rounded
# The sheets this repository ships, by sha256. A run on one of them is labelled synthetic data; any other sheet, an
# edited copy included, is a sheet supplied by the user. A test checks these against the files.
SHIPPED_SHEETS = {
    "customers.synthetic.csv": "d830d47ce1b1b1135af79e754164f7d831d8468f49ff729c5ee3b69462b523c3",
    "fixtures/customers.six-rows.csv": "a2b58e1a7ff11d06cad8c4d96ee392cc9d3f2eec128e5e4e9441c2c00c02a85f",
    "gallery/bad-sheet.csv": "c685886ee74079c11b13e8ea4563e0eb182975ea551d4a52dc557d994ae0625f",
    "gallery/leak-sheet.csv": "eab2a031525fb08a35e0bcbe22af8adc4843c466fa7cf3a8e357ba31ce0f8b31",
}
SHIPPED_META = HERE / "recorded-answer.meta.json"  # the recording the label "Recorded" is earned against
API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_API_BASE = "https://api.anthropic.com"
MAX_TOKENS = 4096
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
BUCKETS = ("current", "1-30", "31-60", "61-90", "over 90")
DEFAULT_WINDOW_DAYS = 7
HISTORY_KEY_PREFIX = "ai-collections-followup-demo history key v2"
HISTORY_FIELDS = ("customer_key", "exported_on", "total", "request_sha256")

PLAIN_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?")   # 4200 or 4200.50; not 4,200, -5 or 1e3
WHOLE_NUMBER = re.compile(r"[0-9]+")                 # 45; not 45.0
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")     # a figure in a text: 950, 12,800 or 12800.50
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # 2026-11-24
LOOSE_STAND_IN = re.compile(r"(?<![\w.,])0[0-9]+")  # 005 or 016 on its own; not 0.05, 1,050 or Customer 005
WEB_ADDRESS = re.compile(r"(?i)\bhttps?://\S*|\bwww\.\S*|\b(?:[a-z0-9-]+\.)+(?:com|net|org|info|io|co|uk|il)\b\S*")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Text shaped like a stand-in, such as "Customer 003" or "ID 777". Whether it is one is decided by this run's key,
# never by the shape: a shaped token that is not in the key is an unknown stand-in.
STAND_IN_SHAPE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(label) for label in pseudonymise.PERSONAL.values()) + r") \d+(?!\w)")


class PipelineError(Exception):
    """A refusal with an exit code: 2 for a usage or setup problem, 3 for an answer that cannot be used."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def money(value):
    """An amount in plain notation: no exponent, no trailing zeros in the fraction (12800, 12345.67, 0.3)."""
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def ageing_bucket(days):
    """The ageing bucket of a delay in days: current (0), 1-30, 31-60, 61-90 or over 90."""
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "over 90"


def read_file(path, what):
    """The text of a file, or a refusal (exit 2) that names it."""
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PipelineError(f"cannot read {what}: {exc}", 2)


def load_sheet(path):
    """Read the sheet and check it before anything is sent. Refused (exit 2): a missing or unreadable file, a missing
    column (the names must match exactly), an empty name, an amount that is not a plain decimal number of 0 or more,
    a delay that is not a whole number of 0 or more."""
    try:
        with pathlib.Path(path).open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows, columns = list(reader), list(reader.fieldnames or [])
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PipelineError(f"cannot read the sheet: {exc}", 2)
    for col in MODEL_COLUMNS:
        if col not in columns:
            like = [c for c in columns if c.casefold() == col.casefold()]
            has = f"it has {like[0]!r}; " if like else ""
            raise PipelineError(f"the sheet has no column {col!r} ({has}column names must match exactly)", 2)
    for col in columns:
        for known in KNOWN_COLUMNS:
            if col != known and col.casefold() == known.casefold():
                raise PipelineError(f"the sheet has a column {col!r} where {known!r} is expected (column names must "
                                    "match exactly)", 2)
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        where = f"row {number} of the sheet"
        if not (row.get(NAME) or "").strip():
            raise PipelineError(f"{where}: the name is empty", 2)
        amount = (row.get(AMOUNT) or "").strip()
        if not PLAIN_AMOUNT.fullmatch(amount):
            raise PipelineError(f"{where}: {AMOUNT} {row.get(AMOUNT)!r} is not a plain decimal number of 0 or more", 2)
        if len(amount.split(".")[0]) > MAX_WHOLE_DIGITS:
            raise PipelineError(f"{where}: {AMOUNT} {amount!r} has more than {MAX_WHOLE_DIGITS} digits before the "
                                "point", 2)
        if not WHOLE_NUMBER.fullmatch((row.get(DAYS) or "").strip()):
            raise PipelineError(f"{where}: {DAYS} {row.get(DAYS)!r} is not a whole number of 0 or more", 2)
    return rows, columns


def model_table(clean_rows):
    """The CSV text the model sees: the four needed columns of the pseudonymised rows, nothing else. A line break
    inside a cell becomes a space; a quote is kept, as CSV quoting."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(MODEL_COLUMNS)
    for row in clean_rows:
        writer.writerow([re.sub(r"\r\n|\r|\n", " ", row.get(col) or "") for col in MODEL_COLUMNS])
    return buffer.getvalue()


def build_request(prompt, clean_rows):
    return prompt.strip() + "\n\nThe table:\n\n" + model_table(clean_rows)


def stand_ins_in(text, key):
    """The stand-in shaped tokens of a text, as two sets: those in this run's key, and the unknown ones."""
    found = {m.group(0) for m in STAND_IN_SHAPE.finditer(text)}
    return found & key.keys(), found - key.keys()


def without_tokens(text, stand_ins):
    """The text with the given stand-ins (exact tokens) taken out."""
    for token in stand_ins:
        text = re.sub(r"(?<!\w)" + re.escape(token) + r"(?!\w)", " ", text)
    return text


def numbers_in(text, stand_ins):
    """The figures a text states in digits, after the given stand-ins (exact tokens, whose numbering is not a figure)
    are taken out. Any other digits count, so an unknown stand-in cannot hide a figure."""
    return {Decimal(n.replace(",", "")) for n in NUMBER.findall(without_tokens(text, stand_ins))}


def loose_problem(text, stand_ins, what):
    """What every checked text must not hold: a numeral other than the digits 0 to 9 (a circled or a full-width
    digit, which the figure check would not read as written), and a number with a leading zero outside an exact
    stand-in, such as 005 in "Customers 005 and 007": a loose reference to a stand-in, which could pass the figure
    check only by chance."""
    odd = sorted({ch for ch in text if ch.isnumeric() and ch not in "0123456789"})
    if odd:
        return f"the {what} holds a numeral other than 0 to 9: " + " ".join(f"U+{ord(ch):04X}" for ch in odd)
    loose = LOOSE_STAND_IN.findall(without_tokens(text, stand_ins))
    if loose:
        return f"the {what} holds a loose reference to a stand-in: " + ", ".join(loose)
    return None


def expected_by_customer(clean_rows, key):
    """Per stand-in, from the sheet itself: the total debt; the largest delay among the rows that still owe money (an
    amount above 0), which the answer's days_late must equal and the ageing bucket comes from; every figure of the
    customer's own rows (each amount and delay, and the total); and the stand-ins those rows hold. A paid row's delay
    is a figure of the customer's, but not their delay."""
    out = {}
    for row in clean_rows:
        amount, days = Decimal(row[AMOUNT].strip()), int(row[DAYS].strip())
        c = out.setdefault(row[NAME], {"total": Decimal(0), "days": 0, "figures": set(), "stand_ins": set()})
        c["total"] += amount
        if amount > 0:
            c["days"] = max(c["days"], days)
        c["figures"] |= {amount, Decimal(days)}
        for value in row.values():
            if isinstance(value, str):
                c["stand_ins"] |= stand_ins_in(value, key)[0]
    for c in out.values():
        c["figures"].add(c["total"])
    return out


def personal_values(rows):
    values = set()
    for row in rows:
        for col in pseudonymise.PERSONAL:
            value = (row.get(col) or "").strip()
            if value:
                values.add(value)
    return values


def iso_date(text):
    """A date written yyyy-mm-dd, for the command line."""
    if not ISO_DATE.fullmatch(text or ""):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date written yyyy-mm-dd")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date in the calendar")


def positive_whole(text):
    """A whole number of 1 or more, for the command line."""
    if not WHOLE_NUMBER.fullmatch(text or "") or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 1 or more")
    return int(text)


def customer_key(name, id_numbers):
    """The run history's key for a customer: a sha256 over a fixed prefix, the name exactly as the pipeline tells
    customers apart by it (the name column as written, without spaces at its ends) and the identity numbers as
    written. Nothing else is normalised, so "Acme Works" and "ACME  Works" are two customers here as everywhere. The
    history file holds no name, but the key is not anonymous: anyone who has the sheet can compute it again."""
    ids = sorted({i.strip() for i in id_numbers} - {""})
    text = "\n".join([HISTORY_KEY_PREFIX, name.strip(), ",".join(ids)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def customer_keys(rows, clean):
    """stand-in -> the customer's history key, from the real name and identity numbers of the sheet."""
    names, ids = {}, {}
    for row, out in zip(rows, clean):
        names[out[NAME]] = row[NAME]
        ids.setdefault(out[NAME], set()).add(row.get(ID_NUMBER) or "")
    return {who: customer_key(names[who], ids[who]) for who in names}


def history_problem(record, as_of):
    """Why a parsed history line cannot be used, or None."""
    if not isinstance(record, dict) or sorted(record) != sorted(HISTORY_FIELDS):
        return "it is not a history record (exactly " + ", ".join(HISTORY_FIELDS) + ")"
    if not all(isinstance(record[f], str) for f in HISTORY_FIELDS):
        return "every field must be a text"
    for field in ("customer_key", "request_sha256"):
        if not SHA256_HEX.fullmatch(record[field]):
            return f"{field} is not a sha256 in hex"
    if not PLAIN_AMOUNT.fullmatch(record["total"]):
        return f"total {record['total']!r} is not a plain decimal number"
    try:
        exported = iso_date(record["exported_on"])
    except argparse.ArgumentTypeError as exc:
        return f"exported_on {exc}"
    if exported > as_of:
        return f"exported_on {record['exported_on']} is after the run's date {as_of.isoformat()}"
    return None


def read_history(path, as_of):
    """The run history: one JSON object per line, written by approval.py export. A missing file is an empty history
    (the first export creates it). Any line that is not a complete record, an empty line included, or that is dated
    after the run's date, refuses the run (exit 2) with its line number: nothing is guessed."""
    path = pathlib.Path(path)
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(read_file(path, "the history file").splitlines(), start=1):
        try:
            record = json.loads(line)
        except ValueError:
            record, problem = None, "it is not JSON"
        else:
            problem = history_problem(record, as_of)
        if problem:
            raise PipelineError(f"the history file, line {number}: {problem}", 2)
        records.append(dict(record, date=datetime.date.fromisoformat(record["exported_on"])))
    return records


def latest_inside_window(history, as_of, window_days):
    """customer key -> the latest history line inside the window: exported fewer than window_days days before as_of,
    so ages 0 to window_days - 1. Of two lines of the same date, the later one in the file counts."""
    latest = {}
    for record in history:
        if (as_of - record["date"]).days < window_days:
            seen = latest.get(record["customer_key"])
            if seen is None or record["date"] >= seen["date"]:
                latest[record["customer_key"]] = record
    return latest


def earlier_drafts(history, keys, expected, as_of, window_days):
    """Which owing customers were exported inside the window: the latest such line of each customer counts (a later
    line of the same date wins). Returns two dicts, stand-in -> that line: the customers whose total is the same,
    who are left out of the request, and those whose total changed, who are drafted again and marked."""
    latest = latest_inside_window(history, as_of, window_days)
    drafted, changed = {}, {}
    for who, c in expected.items():
        record = latest.get(keys[who])
        if c["total"] > 0 and record is not None:
            (drafted if Decimal(record["total"]) == c["total"] else changed)[who] = record
    return drafted, changed


def prepare(sheet, prompt_path, history=(), as_of=None, window_days=DEFAULT_WINDOW_DAYS):
    """Everything before the model call: check the sheet, replace the personal columns, leave out the customers the
    history shows as already drafted, build the request, guard it. Nothing is sent here."""
    rows, columns = load_sheet(sheet)
    clean, key = pseudonymise.pseudonymise(rows, columns)
    for row, out in zip(rows, clean):
        # The two figure columns were checked as plain numbers. They go to the model as written, so the scrub of
        # nine-digit numbers can never change an amount.
        out[AMOUNT], out[DAYS] = row[AMOUNT].strip(), row[DAYS].strip()
    everyone = expected_by_customer(clean, key)
    keys = customer_keys(rows, clean)
    drafted, changed = earlier_drafts(history, keys, everyone, as_of or datetime.date.today(), window_days)
    request_text = build_request(read_file(prompt_path, "the prompt"), [r for r in clean if r[NAME] not in drafted])
    # As whole words in any letter case, as the scrub matches them: a short name is not found inside a longer word.
    leaked = sorted(v for v in personal_values(rows) if pseudonymise.whole_word(v).search(request_text))
    if leaked:
        raise PipelineError(f"{len(leaked)} personal value(s) would reach the model; nothing was sent", 2)
    return {"clean": clean, "key": key, "request": request_text, "leaked": leaked,
            "expected": {who: c for who, c in everyone.items() if who not in drafted}, "everyone": everyone,
            "customer_keys": keys, "already_drafted": drafted, "amount_changed": changed}


class RecordedBackend:
    """A saved model answer, read from a file. A missing or unreadable file is refused (exit 2). It keeps the sha256
    of the bytes it replayed, and reads the request_sha256 of the meta file beside it (<name>.meta.json), if any."""

    name = "recorded"

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.model = f"recorded:{self.path.name}"
        self.sha256 = None

    def complete(self, request_text):
        text = read_file(self.path, "the recorded answer")
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return text

    def meta_request_sha256(self):
        """The request_sha256 of the meta file beside the answer, or None when there is no readable one."""
        try:
            meta = json.loads(self.path.with_name(self.path.stem + ".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = meta.get("request_sha256") if isinstance(meta, dict) else None
        return value if isinstance(value, str) else None


class AnthropicBackend:
    """The Anthropic Messages API over HTTPS. The key comes from the environment and is never printed or written.
    Any failure is refused (exit 3) before anything is written."""

    name = "anthropic"

    def __init__(self, model=DEFAULT_MODEL, api_base=DEFAULT_API_BASE, timeout=60):
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.key = os.environ.get(API_KEY_ENV, "")
        if not self.key:
            raise PipelineError(f"no API key in {API_KEY_ENV}; use --backend recorded, or set the variable", 2)

    def complete(self, request_text):
        body = json.dumps({
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": request_text}],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.api_base + "/v1/messages", data=body, method="POST", headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.key,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise PipelineError(f"the API answered with HTTP status {exc.code}", 3)
        except (OSError, http.client.HTTPException) as exc:
            raise PipelineError(f"the API could not be reached: {getattr(exc, 'reason', None) or exc}", 3)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise PipelineError("the API answer is not JSON", 3)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise PipelineError("the API answer is not in the shape of the Messages API", 3)
        if data.get("stop_reason") == "max_tokens":
            raise PipelineError(f"the answer was cut off at max_tokens ({MAX_TOKENS})", 3)
        texts = [b.get("text") for b in data["content"] if isinstance(b, dict) and b.get("type") == "text"]
        if not texts or not all(isinstance(t, str) for t in texts):
            raise PipelineError("the API answer carried no text block", 3)
        return "".join(texts)


def refuse_constant(name):
    raise ValueError(f"{name} is not a number this pipeline accepts")


def parse_answer(text):
    """Accept the JSON object even inside a code fence; refuse anything else. A number with a fraction is read as a
    Decimal, so amounts compare exactly; NaN and Infinity are refused."""
    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        data = json.loads(body, parse_float=Decimal, parse_constant=refuse_constant)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"the answer is not JSON: {exc.msg} at character {exc.pos}", 3)
    except ValueError as exc:
        raise PipelineError(f"the answer cannot be used: {exc}", 3)
    if not isinstance(data, dict) or not isinstance(data.get("call_list"), list):
        raise PipelineError("the answer has no call_list, or its call_list is not a list", 3)
    if not isinstance(data.get("for_a_human", []), list):
        raise PipelineError("for_a_human is not a list", 3)
    return data


def is_amount(value):
    """A finite number from the JSON: an int or a Decimal, not a bool and not a string."""
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool) and Decimal(value).is_finite()


def is_whole(value):
    """A JSON integer: not a bool, and not a number written with a fraction such as 90.0."""
    return isinstance(value, int) and not isinstance(value, bool)


def listed(values, show=str):
    return ", ".join(show(v) for v in sorted(values))


def reminder_problem(reminder, who, c, key):
    """A reminder goes to one customer: it may name only that customer, may state only the total and the days late,
    and must state the total."""
    if not isinstance(reminder, str) or not reminder.strip():
        return "the reminder is missing or empty"
    known, unknown = stand_ins_in(reminder, key)
    if unknown:
        return "the reminder holds an unknown stand-in: " + listed(unknown)
    if known - {who}:
        return "the reminder names a stand-in other than this customer: " + listed(known - {who})
    problem = loose_problem(reminder, {who}, "reminder")
    if problem:
        return problem
    if "%" in reminder:
        return "the reminder holds a per cent sign"
    web = WEB_ADDRESS.search(reminder)
    if web:
        return f"the reminder holds a web address: {web.group(0).rstrip('.,;:!?)')}"
    figures = numbers_in(reminder, {who})
    extra = figures - {c["total"], Decimal(c["days"])}
    if extra:
        return "the reminder states a figure other than the total and the days late: " + listed(extra, money)
    if c["total"] not in figures:
        return f"the reminder does not state the total {money(c['total'])}"
    return None


def reason_problem(reason, c, key):
    """A reason may name only this customer and the stand-ins in their own rows, and state only their own figures."""
    if not isinstance(reason, str):
        return "the reason is missing or not a text"
    known, unknown = stand_ins_in(reason, key)
    if unknown:
        return "the reason holds an unknown stand-in: " + listed(unknown)
    if known - c["stand_ins"]:
        return "the reason names a stand-in from outside this customer's rows: " + listed(known - c["stand_ins"])
    problem = loose_problem(reason, known, "reason")
    if problem:
        return problem
    extra = numbers_in(reason, known) - c["figures"]
    if extra:
        return "the reason states a figure that is not one of this customer's: " + listed(extra, money)
    return None


def note_problem(note, sheet_figures, key):
    """A note for a person may name any stand-in of this run, and state only figures that are in the sheet."""
    if not isinstance(note, str):
        return "the note is not a text"
    known, unknown = stand_ins_in(note, key)
    if unknown:
        return "the note holds an unknown stand-in: " + listed(unknown)
    problem = loose_problem(note, known, "note")
    if problem:
        return problem
    extra = numbers_in(note, known) - sheet_figures
    if extra:
        return "the note states a figure that is not in the sheet: " + listed(extra, money)
    return None


def item_problem(item, expected, seen, priorities, key):
    """Why a call-list item cannot be accepted, or None when every check passes."""
    who = item.get("customer")
    amount, days, priority = item.get("amount"), item.get("days_late"), item.get("priority")
    if not isinstance(who, str) or who not in expected:
        return "this customer is not in the sheet"
    c = expected[who]
    if c["total"] <= 0:
        return "the sheet shows nothing owed"
    if who in seen:
        return "the same customer appears twice"
    if not is_amount(amount):
        return "the amount is missing or not a finite number"
    if not is_whole(days):
        return "days_late is missing or not a whole number"
    if not is_whole(priority):
        return "priority is missing or not a whole number"
    if amount != c["total"]:
        return f"the amount {money(amount)} does not match the sheet's total {money(c['total'])}"
    if days != c["days"]:
        return f"days late {days} does not match the sheet's {c['days']}"
    if priority < 1 or priority in priorities:
        return "the priority is repeated or below 1"
    return reminder_problem(item.get("reminder"), who, c, key) or reason_problem(item.get("reason"), c, key)


def validate(answer, expected, key):
    """Split the call list into accepted and rejected items, each rejection with its reason, and list who is missing.
    Split the notes for a person into accepted and rejected the same way.
    Returns (accepted, rejected, missing, notes, rejected_notes)."""
    accepted, rejected, seen, priorities = [], [], set(), set()
    for item in answer["call_list"]:
        if not isinstance(item, dict):
            rejected.append({"item": str(item)[:80], "reason": "not an object"})
            continue
        problem = item_problem(item, expected, seen, priorities, key)
        if problem:
            rejected.append({"customer": str(item.get("customer", ""))[:80], "reason": problem})
            continue
        who = item["customer"]
        seen.add(who)
        priorities.add(item["priority"])
        # The amount kept is the sheet's own total, which the check has just proved equal to the model's.
        accepted.append({"customer": who, "priority": item["priority"], "amount": expected[who]["total"],
                         "days_late": item["days_late"], "reason": item["reason"], "reminder": item["reminder"]})
    accepted.sort(key=lambda a: a["priority"])
    owing = {who for who, c in expected.items() if c["total"] > 0}
    missing = sorted(owing - seen - {r.get("customer") for r in rejected})
    sheet_figures = set().union(*(c["figures"] for c in expected.values()))
    notes, rejected_notes = [], []
    for note in answer.get("for_a_human", []):
        problem = note_problem(note, sheet_figures, key)
        if problem:
            rejected_notes.append({"note": str(note), "reason": problem})
        else:
            notes.append(note)
    return accepted, rejected, missing, notes, rejected_notes


def page_for_a_person(rejected, missing, notes, rejected_notes, key, history=None, guarded=0):
    """The page a person reads after the run, with the real names restored. It stays on this computer. With a run
    history, history = (already_drafted, amount_changed, window_days, the sheet's customers) adds two sections.
    guarded is how many cells of call-list.csv were written with a leading apostrophe (safe_cells)."""
    def section(title, entries):
        return [title] + ([f"- {e}" for e in entries] or ["- none"]) + [""]

    lines = ["For a person to read. It holds real names: keep it on this computer.", ""]
    lines += section("Notes from the model:", notes)
    lines += section("Items rejected by the checks (not in the call list):",
                     [f"{r.get('customer', r.get('item'))}: {r['reason']}" for r in rejected])
    lines += section("Customers who owe money and are missing from the answer:", missing)
    lines += section("Notes rejected by the checks:", [f"{r['note']} ({r['reason']})" for r in rejected_notes])
    if history is not None:
        drafted, changed, window_days, everyone, answered = history
        lines += section(f"Already drafted in the last {window_days} days, with the same total (left out of the "
                         "request):", [f"{who}: drafted on {r['exported_on']}, total {r['total']}"
                                       for who, r in sorted(drafted.items())])
        lines += section("Drafted again: the amount changed since the last draft:",
                         [f"{who}: {money(everyone[who]['total'])} now, {r['total']} when drafted on "
                          f"{r['exported_on']}" for who, r in sorted(changed.items())])
        lines += section("Answered although already drafted (from an answer to another request; set aside, not "
                         "counted as refused):", answered)
    lines += [f"Cells of call-list.csv written with a leading apostrophe, so that a spreadsheet shows them as text and "
              f"does not run them as formulas: {guarded}"]
    return pseudonymise.restore("\n".join(lines), key)


def check_webhook(url, allow_remote):
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in LOCAL_HOSTS and not allow_remote:
        raise PipelineError(f"refusing a webhook outside this computer ({host}); pass --allow-remote-webhook only "
                            "with the approval of the owner of the data", 2)


def post_webhook(url, payload, allow_remote=False, timeout=15):
    """POST the payload and return the HTTP status. A failure raises OSError or http.client.HTTPException."""
    check_webhook(url, allow_remote)
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def webhook_result(url, payload, allow_remote):
    """The HTTP status on success, or the text "failed: <short reason>"; the run's outputs are kept either way."""
    try:
        status = post_webhook(url, payload, allow_remote)
    except urllib.error.HTTPError as exc:
        exc.close()
        return f"failed: HTTP {exc.code}"
    except (OSError, http.client.HTTPException) as exc:
        return f"failed: {getattr(exc, 'reason', None) or exc}"
    return status if 200 <= status < 300 else f"failed: HTTP {status}"


def bucket_of(c):
    """The ageing bucket of one customer of expected_by_customer."""
    return ageing_bucket(c["days"])


def ageing(expected):
    """What is owed, per bucket: the owing customers (stand-ins) and their total."""
    out = []
    for bucket in BUCKETS:
        who = sorted(w for w, c in expected.items() if c["total"] > 0 and bucket_of(c) == bucket)
        out.append({"bucket": bucket, "customers": who,
                    "total": money(sum((expected[w]["total"] for w in who), Decimal(0)))})
    return out


def api_host(api_base):
    """The host (and port) an API address points at, without its scheme, path or anything else."""
    parts = urllib.parse.urlparse(api_base)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return host + (f":{parts.port}" if parts.port else "")


def shipped_recording():
    """The answer_sha256 and request_sha256 of the shipped recording's meta file; None for what cannot be read."""
    try:
        meta = json.loads(SHIPPED_META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    return meta.get("answer_sha256"), meta.get("request_sha256")


def write_text(path, text):
    """Write UTF-8 with LF line ends on every system, so that a file's sha256 does not depend on the system that
    wrote it, and return that sha256."""
    data = text.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    return write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def run(sheet, prompt_path, backend, out_dir, webhook=None, allow_remote_webhook=False, history=None, as_of=None,
        window_days=DEFAULT_WINDOW_DAYS):
    """One run. history is the run history file, or None for no history (every owing customer is asked about);
    as_of is the run's date, today when None."""
    out_dir = pathlib.Path(out_dir)
    if out_dir.exists():
        raise PipelineError(f"refusing to write into an existing folder: {out_dir}", 2)
    if webhook:
        check_webhook(webhook, allow_remote_webhook)
    as_of = as_of or datetime.date.today()
    records = read_history(history, as_of) if history is not None else []
    prep = prepare(sheet, prompt_path, records, as_of, window_days)
    key = prep["key"]
    drafted, changed = prep["already_drafted"], prep["amount_changed"]
    # When the history shows every owing customer as already drafted, there is nothing to ask the model.
    model_called = not drafted or any(c["total"] > 0 for c in prep["expected"].values())
    answer = parse_answer(backend.complete(prep["request"])) if model_called else {"call_list": [], "for_a_human": []}
    # An item for a customer the history left out of the request cannot be a mistake about this request: it comes from
    # an answer to another one (a recording replayed after an export). It is listed apart and not counted as refused.
    def left_out(item):
        return isinstance(item, dict) and isinstance(item.get("customer"), str) and item["customer"] in drafted

    answered_drafted = sorted({item["customer"] for item in answer["call_list"] if left_out(item)})
    answer = dict(answer, call_list=[item for item in answer["call_list"] if not left_out(item)])
    accepted, rejected, missing, notes, rejected_notes = validate(answer, prep["expected"], key)

    def real(text):
        return pseudonymise.restore(text, key)

    out_dir.mkdir(parents=True)
    guarded = 0
    with (out_dir / "call-list.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["priority", "customer", "amount", "days_late", "bucket", "reason"])
        for a in accepted:
            cells, changed_cells = safe_cells([a["priority"], real(a["customer"]), money(a["amount"]), a["days_late"],
                                               bucket_of(prep["expected"][a["customer"]]), real(a["reason"])])
            w.writerow(cells)
            guarded += changed_cells
    # The drafts, each with the sha256 of its bytes as written: approval.py refuses to export a changed draft.
    drafts_sha256 = {"call-list.csv": hashlib.sha256((out_dir / "call-list.csv").read_bytes()).hexdigest()}
    drafts = []
    (out_dir / "reminders").mkdir()
    for a in accepted:
        name = f"reminders/reminder-{a['priority']:02d}.txt"
        drafts_sha256[name] = write_text(out_dir / name, real(a["reminder"]) + "\n")
        drafts.append({"item": a["priority"], "customer": a["customer"], "amount": money(a["amount"]),
                       "days_late": a["days_late"], "bucket": bucket_of(prep["expected"][a["customer"]]),
                       "file": name, "customer_key": prep["customer_keys"][a["customer"]],
                       "amount_changed": a["customer"] in changed})
    page_history = (drafted, changed, window_days, prep["everyone"], answered_drafted) if history is not None else None
    write_text(out_dir / "for-a-person.txt",
               page_for_a_person(rejected, missing, notes, rejected_notes, key, page_history, guarded) + "\n")
    # The same content, with the real names, in a form report.py can read without parsing the page.
    write_json(out_dir / "for-a-person.json", {
        "note": "For a person to read. It holds real names: keep it on this computer.",
        "csv_cells_guarded": guarded,
        "for_a_human": [real(n) for n in notes],
        "rejected": [{"customer": real(str(r.get("customer", r.get("item")))), "reason": real(r["reason"])}
                     for r in rejected],
        "missing_from_answer": [real(who) for who in missing],
        "rejected_notes": [{"note": real(r["note"]), "reason": real(r["reason"])} for r in rejected_notes],
        "already_drafted": [{"customer": real(who), "drafted_on": r["exported_on"], "total": r["total"]}
                            for who, r in sorted(drafted.items())],
        "amount_changed": [{"customer": real(who), "total": money(prep["everyone"][who]["total"]),
                            "last_total": r["total"], "last_drafted_on": r["exported_on"]}
                           for who, r in sorted(changed.items())],
        "answered_although_drafted": [real(who) for who in answered_drafted],
    })
    owing = {who: c for who, c in prep["everyone"].items() if c["total"] > 0}
    api_base = getattr(backend, "api_base", None)
    request_sha256 = hashlib.sha256(prep["request"].encode("utf-8")).hexdigest()
    sheet_sha256 = hashlib.sha256(pathlib.Path(sheet).read_bytes()).hexdigest()
    answer_sha256 = getattr(backend, "sha256", None)
    shipped_answer, shipped_request = shipped_recording()
    report = {
        "backend": backend.name,
        "model": backend.model,
        # The facts the report page's labels are earned from: what was replayed, and on which sheet.
        "answer_sha256": answer_sha256,
        "answer_is_shipped_recording": answer_sha256 is not None and answer_sha256 == shipped_answer,
        "request_is_shipped_recordings": request_sha256 == shipped_request,
        "sheet_sha256": sheet_sha256,
        "sheet_is_shipped": sheet_sha256 in SHIPPED_SHEETS.values(),
        # Where the API requests went, so that a run against a mock or another endpoint is never shown as live.
        "api_host": api_host(api_base) if api_base else None,
        "api_default_address": api_base == DEFAULT_API_BASE if api_base else None,
        "request_sha256": request_sha256,
        "personal_values_in_request": len(prep["leaked"]),
        "customers_owing": len(owing),
        "accepted": len(accepted),
        "rejected": rejected,
        "missing_from_answer": missing,
        "for_a_human": notes,
        "rejected_notes": rejected_notes,
        "sent_to_customers": 0,  # there is no send step
        "as_of": as_of.isoformat(),
        "model_called": model_called,
        "total_owed": money(sum((c["total"] for c in owing.values()), Decimal(0))),
        "ageing": ageing(prep["everyone"]),
        "history_lines": len(records) if history is not None else None,
        "window_days": window_days,
        "already_drafted": [{"customer": who, "drafted_on": r["exported_on"], "total": r["total"]}
                            for who, r in sorted(drafted.items())],
        "amount_changed": [{"customer": who, "total": money(prep["everyone"][who]["total"]),
                            "last_total": r["total"], "last_drafted_on": r["exported_on"]}
                           for who, r in sorted(changed.items())],
        "answered_although_drafted": answered_drafted,
        "answer_meta_request_sha256": backend.meta_request_sha256() if hasattr(backend, "meta_request_sha256")
        else None,
        "drafts": drafts,
        "drafts_sha256": drafts_sha256,
    }
    write_json(out_dir / "run-report.json", report)
    if webhook:
        # Stand-ins only. Amounts are strings in plain notation, so no figure is rounded on the way.
        summary = {"event": "collections-run", "accepted": [{"customer": a["customer"], "priority": a["priority"],
                   "amount": money(a["amount"])} for a in accepted], "rejected": len(rejected), "missing": missing}
        report["webhook_status"] = webhook_result(webhook, summary, allow_remote_webhook)
        write_json(out_dir / "run-report.json", report)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sheet", default=str(HERE / "customers.synthetic.csv"))
    ap.add_argument("--prompt", default=str(HERE / "prompt.txt"))
    ap.add_argument("--backend", choices=["recorded", "anthropic"], default="recorded")
    ap.add_argument("--recorded", default=str(HERE / "recorded-answer.json"), help="the recorded model answer")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--out", help="a folder that does not exist yet (not needed with --print-request)")
    ap.add_argument("--webhook", help="an automation trigger URL on this computer (n8n or Make style)")
    ap.add_argument("--allow-remote-webhook", action="store_true")
    ap.add_argument("--print-request", action="store_true", help="print the exact text the model would get, then stop")
    ap.add_argument("--history", help="the run history (JSON lines, written by approval.py export); without it, "
                                      "every owing customer is asked about, as before")
    ap.add_argument("--as-of", type=iso_date, help="the run's date, yyyy-mm-dd (default: today)")
    ap.add_argument("--window-days", type=positive_whole, default=DEFAULT_WINDOW_DAYS,
                    help="a customer exported this many days ago or less, with the same total, is not drafted again")
    args = ap.parse_args(argv)
    if not args.out and not args.print_request:
        ap.error("--out is required, except with --print-request")
    try:
        if args.print_request:
            as_of = args.as_of or datetime.date.today()
            records = read_history(args.history, as_of) if args.history is not None else []
            request_text = prepare(args.sheet, args.prompt, records, as_of, args.window_days)["request"]
            sys.stdout.flush()
            sys.stdout.buffer.write(request_text.encode("utf-8"))  # the exact bytes: UTF-8, LF line ends
            sys.stdout.buffer.flush()
            return 0
        if args.backend == "recorded":
            backend = RecordedBackend(args.recorded)
        else:
            backend = AnthropicBackend(args.model, args.api_base)
        report = run(args.sheet, args.prompt, backend, args.out, args.webhook, args.allow_remote_webhook,
                     args.history, args.as_of, args.window_days)
    except PipelineError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    status = report.get("webhook_status")
    line = (f"backend {report['backend']} | owing {report['customers_owing']} | accepted {report['accepted']} | "
            f"rejected {len(report['rejected'])} | missing {len(report['missing_from_answer'])} | "
            f"rejected notes {len(report['rejected_notes'])} | sent to customers {report['sent_to_customers']}")
    if report["history_lines"] is not None:
        line += (f" | history lines {report['history_lines']} | already drafted {len(report['already_drafted'])}"
                 f" | amount changed {len(report['amount_changed'])} | answered although drafted "
                 f"{len(report['answered_although_drafted'])}")
    if not report["model_called"]:
        line += " | model not called: every owing customer was already drafted"
    if status is not None:
        line += f" | webhook {status}"
    print(line)
    meta_request = report["answer_meta_request_sha256"]
    if report["model_called"] and meta_request is not None and meta_request != report["request_sha256"]:
        print("note: the replayed answer was saved for another request (its meta file's request_sha256 is not this "
              "run's), so items for customers outside this request are listed apart")
    return 4 if isinstance(status, str) else 0


if __name__ == "__main__":
    sys.exit(main())
