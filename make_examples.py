"""Write again every file in this repository that is made from other files, so that none of them can drift:

- gallery/: the constructed wrong answers and broken sheets of the failure gallery, each the recorded answer or the
  shipped sheet with one thing changed (gallery/README.md, written by hand, says what);
- examples/: the real output of demo.py --case happy-path for the committed seed, a fixed --as-of and a fixed --at;
- docs/img/demo.svg: the closing table of a real run of the whole demo, drawn by tools/term_svg.py;
- docs/TESTS.md: the list of the tests, from their names.

CI runs this and fails if git then shows any difference. Nothing in examples/ is edited by hand.
Standard library only. Run: python -I make_examples.py
Exit codes: 0 written; 1 a demo run did not end as expected (examples/ and the picture are left as they were).
"""
import argparse
import contextlib
import copy
import csv
import io
import json
import pathlib
import subprocess
import sys
import tempfile
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE / "tools"))
import demo  # noqa: E402
import make_sheet  # noqa: E402
import pipeline  # noqa: E402
import pseudonymise  # noqa: E402
import run_tests  # noqa: E402
import term_svg  # noqa: E402

AS_OF = "2026-11-24"
AT = "2026-11-24T09:00:00Z"
REMINDERS_SHOWN = 3
EXAMPLES = HERE / "examples"
GALLERY = HERE / "gallery"
DOCS_TESTS = HERE / "docs" / "TESTS.md"
PICTURE = HERE / "docs" / "img" / "demo.svg"
PICTURE_COMMAND = "python -I demo.py --out out-demo"


def json_ready(value):
    """A parsed answer with every Decimal turned into a JSON number that reads back as the same Decimal."""
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        if Decimal(repr(float(value))) != value:
            raise ValueError(f"{value} does not survive the trip through a JSON number")
        return float(value)
    return value


def answer_text(answer):
    return json.dumps(json_ready(answer), ensure_ascii=False, indent=1) + "\n"


def sheet_rows():
    with demo.SHEET.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def invented_stand_in(rows):
    """A stand-in one past the sheet's last customer: the sheet cannot have it."""
    return f"{pseudonymise.PERSONAL['name']} {len({r['name'].strip() for r in rows}) + 1:03d}"


def gallery_files():
    """gallery/ file name -> bytes, for every constructed file of the gallery."""
    answer = pipeline.parse_answer(demo.RECORDED.read_text(encoding="utf-8"))
    rows = sheet_rows()
    files = {}

    wrong = copy.deepcopy(answer)
    item = next(i for i in wrong["call_list"] if isinstance(i, dict) and i.get("priority") == 2)
    item["amount"] = item["amount"] + 100
    files["wrong-total.json"] = answer_text(wrong)

    invented = copy.deepcopy(answer)
    who = invented_stand_in(rows)
    last = max(i["priority"] for i in invented["call_list"] if isinstance(i, dict) and pipeline.is_whole(
        i.get("priority")))
    invented["call_list"].append({
        "customer": who, "priority": last + 1, "amount": 1000, "days_late": 30,
        "reason": "A balance of 1,000; the oldest open invoice is 30 days late.",
        "reminder": f"Dear {who}, our records show an outstanding balance of 1,000, now 30 days late. Thank you."})
    files["invented-customer.json"] = answer_text(invented)

    bad = [dict(row) for row in rows]
    bad[0]["amount_due"] = "4,200"
    files["bad-sheet.csv"] = make_sheet.sheet_text(bad)

    leak = [dict(row) for row in rows]
    first_word, rest = leak[0]["name"].split(" ", 1)
    leak[0]["notes"] = f"Ask {first_word}\n{rest} to call back"
    files["leak-sheet.csv"] = make_sheet.sheet_text(leak)
    return {name: text.encode("utf-8") for name, text in files.items()}


def what_the_checks_did(report):
    """Lines that say what the checks did with the recorded answer, from the run report."""
    lines = [f"What the checks did with it: {report['accepted']} of {report['customers_owing']} owing customers "
             f"accepted, {len(report['rejected'])} item(s) rejected, {len(report['missing_from_answer'])} missing, "
             f"and {len(report['rejected_notes'])} note(s) for a person set aside."]
    if report["rejected_notes"]:
        lines += ["", "The note(s) set aside, as the run report keeps them (with stand-ins), and the reason:", ""]
        lines += [f"- \"{r['note']}\" ({r['reason']})" for r in report["rejected_notes"]]
        lines += ["", "A note may state only figures that are in the sheet, and a stand-in counts only when it is "
                      "written in full, such as `Customer 019`. Written any other way, its number reads as a figure "
                      "the check cannot confirm, and the note goes to a person as set aside instead of passing as "
                      "checked. The check is strict on purpose: it would rather show a person a note it cannot "
                      "confirm, even one that is right in substance, as the sheet's own notes on the phone show this "
                      "one to be."]
    return lines


def examples_readme(run, reminders, demo_line):
    meta = json.loads((HERE / "recorded-answer.meta.json").read_text(encoding="utf-8"))
    report = json.loads((run / "run-report.json").read_text(encoding="utf-8"))
    decisions = json.loads((run / "decisions.json").read_text(encoding="utf-8"))["decisions"]
    decided = {kind: [str(e["item"]) for e in decisions if e["decision"] == kind] for kind in ("approved", "rejected")}
    return "\n".join([
        "# Examples", "",
        "Everything in this folder is written by `python -I make_examples.py`, and nothing in it is edited by hand: "
        "CI writes it again and fails if git then shows a difference.", "",
        f"It is the real output of `python -I demo.py --case happy-path --as-of {AS_OF} --at {AT}` on the shipped "
        f"sheet (`make_sheet.py`, seed {make_sheet.DEFAULT_SEED}, {make_sheet.DEFAULT_CUSTOMERS} customers) with "
        f"`recorded-answer.json`: one real answer from `{meta['model']}`, recorded on {meta['recorded_on']} "
        f"({meta['route']}).", ""] + what_the_checks_did(report) + ["",
        "The demo printed:", "", "```", demo_line, "```", "",
        "| File | What it is |", "|---|---|",
        "| `call-list.csv` | The call list the run drafted, in priority order, with the (invented) names and each "
        "customer's ageing bucket |",
        f"| `reminders/` | The first {REMINDERS_SHOWN} of the run's {reminders} reminder drafts |",
        "| `for-a-person.txt` | The page for a person: the model's notes, what the checks refused, who is missing |",
        "| `run-report.json` | The run report: stand-ins only, the ageing, and the sha256 of every draft as written |",
        f"| `decisions.json` | Items {' and '.join(decided['approved'])} approved, item "
        f"{' and '.join(decided['rejected'])} rejected with a reason; every other item is pending |",
        "| `approved/` | The export: only the approved call-list lines and reminders |",
        "| `history.jsonl` | The run history the export wrote: one line per approved customer, and no name |",
        "| `report.html` | The report page, written after the decisions and the export |", ""])


def example_files():
    """examples/ path -> bytes, from a fresh run of the happy-path case; None when the case did not end as expected."""
    with tempfile.TemporaryDirectory() as tmp:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = demo.main(["--case", "happy-path", "--out", tmp, "--as-of", AS_OF, "--at", AT])
        if code != 0:
            print(printed.getvalue(), end="", file=sys.stderr)
            return None
        case = pathlib.Path(tmp) / "demo-001" / "happy-path"
        run = case / "run"
        files = {name: (run / name).read_bytes()
                 for name in ("call-list.csv", "for-a-person.txt", "run-report.json", "decisions.json")}
        reminders = sorted((run / "reminders").iterdir())
        files.update({f"reminders/{p.name}": p.read_bytes() for p in reminders[:REMINDERS_SHOWN]})
        files.update({"approved/" + p.relative_to(run / "approved").as_posix(): p.read_bytes()
                      for p in sorted((run / "approved").rglob("*")) if p.is_file()})
        files["history.jsonl"] = (case / "history.jsonl").read_bytes()
        files["report.html"] = (case / "report.html").read_bytes()
        files["README.md"] = examples_readme(run, len(reminders), printed.getvalue().splitlines()[0]).encode("utf-8")
        return files


def demo_picture():
    """docs/img/demo.svg as bytes: the closing table and the total line of a real run of the whole demo, drawn by
    tools/term_svg.py under the command that printed them. The demo runs as its own process in an empty folder, exactly
    as PICTURE_COMMAND says; None when it did not end as expected."""
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run([sys.executable, "-I", str(HERE / "demo.py"), "--out", "out-demo"], cwd=tmp,
                              capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        print(done.stdout + done.stderr, end="", file=sys.stderr)
        return None
    lines = done.stdout.splitlines()
    closing = lines[lines.index("") + 1:]  # after the detailed lines and the blank line that ends them
    svg, cut = term_svg.draw(["$ " + PICTURE_COMMAND] + closing, PICTURE_COMMAND, demo.COMPACT_WIDTH)
    if cut:
        raise ValueError(f"{cut} line(s) of the closing table are wider than {demo.COMPACT_WIDTH} characters")
    return svg.encode("utf-8")


def sync(folder, files):
    """Make the folder hold exactly these files (path -> bytes): write each, then remove any other file and any
    folder left empty. Only paths inside the folder are touched."""
    for name, data in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        name = path.relative_to(folder).as_posix()
        if path.is_file() and name not in files:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    gallery = gallery_files()
    for name, data in gallery.items():
        (GALLERY / name).write_bytes(data)
    examples = example_files()
    picture = demo_picture()
    if examples is None or picture is None:
        print("REFUSED: a demo run did not end as expected; examples/ and the picture were left as they were",
              file=sys.stderr)
        return 1
    sync(EXAMPLES, examples)
    PICTURE.parent.mkdir(parents=True, exist_ok=True)
    PICTURE.write_bytes(picture)
    DOCS_TESTS.parent.mkdir(exist_ok=True)
    pipeline.write_text(DOCS_TESTS, run_tests.markdown())
    print(f"gallery: {len(gallery)} files | examples: {len(examples)} files | docs/img/demo.svg | docs/TESTS.md: "
          f"{len(list(run_tests.cases(run_tests.suite())))} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
