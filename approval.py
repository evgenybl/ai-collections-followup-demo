"""Record a person's decision on each draft of a run, and export only what was approved.

A run folder written by pipeline.py holds drafts: the call list and one reminder per accepted customer. Nothing in it
goes anywhere until a person decides:

  python -I approval.py list    <run folder>
  python -I approval.py approve <run folder> --item <priority> --by <name> [--at <timestamp>]
  python -I approval.py reject  <run folder> --item <priority> --by <name> --reason <text> [--at <timestamp>]
  python -I approval.py export  <run folder> [--history <file>] [--as-of <yyyy-mm-dd>]

An item is one line of the call list with its reminder, named by its priority. Decisions live in decisions.json in
the run folder, with the run's request_sha256 and one sha256 over the drafts as they were written. Entries are
appended, never rewritten: a changed mind is a new entry, and the last entry for an item counts. --by records who a
person says they are; this tool does not authenticate anyone. Timestamps are UTC, ISO 8601 (2026-11-24T09:00:00Z);
--at sets one, so that an example can be reproduced.

export writes approved/ inside the run folder (the approved reminders and an approved call list) and appends one line
per approved customer to the run history, so that the next run does not draft them again. It refuses, and writes
nothing, when decisions.json names another run, when a draft was changed after it was written, or when approved/
already exists. An item with no decision is pending and is not exported; with nothing approved, nothing is exported.
There is still no step that sends anything to a customer.

Standard library only. Exit codes: 0 done (an export with nothing approved included); 2 refused.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline  # noqa: E402
from pipeline import PipelineError  # noqa: E402

DECISIONS = "decisions.json"
APPROVED = "approved"
DRAFT_NAME = re.compile(r"call-list\.csv|reminders/reminder-[0-9]+\.txt")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
ENTRY_FIELDS = ("item", "decision", "by", "at", "reason")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def drafts_digest(manifest):
    """One sha256 over the drafts as written: over the run report's sha256 of each draft, in a fixed order."""
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(TIMESTAMP_FORMAT)


def check_timestamp(text):
    """A UTC timestamp written 2026-11-24T09:00:00Z, or a refusal (exit 2)."""
    if TIMESTAMP.fullmatch(text or ""):
        try:
            datetime.datetime.strptime(text, TIMESTAMP_FORMAT)
            return text
        except ValueError:
            pass
    raise PipelineError(f"--at {text!r} is not a UTC timestamp written yyyy-mm-ddThh:mm:ssZ", 2)


def load_run(folder):
    """The run report of a run folder, with the record of its drafts; a refusal (exit 2) when it is not one."""
    path = pathlib.Path(folder) / "run-report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError(f"not a run folder: there is no run-report.json in {folder}", 2)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read run-report.json: {exc}", 2)
    if not (isinstance(report, dict) and isinstance(report.get("request_sha256"), str)
            and isinstance(report.get("drafts"), list) and isinstance(report.get("drafts_sha256"), dict)):
        raise PipelineError("run-report.json holds no record of its drafts; run the pipeline again", 2)
    for name in report["drafts_sha256"]:
        if not DRAFT_NAME.fullmatch(name):
            raise PipelineError(f"run-report.json names an unexpected draft file: {name!r}", 2)
    for draft in report["drafts"]:
        if not (isinstance(draft, dict) and type(draft.get("item")) is int
                and draft.get("file") in report["drafts_sha256"]):
            raise PipelineError("run-report.json holds a draft record that does not match its files", 2)
    return report


def changed_drafts(folder, report):
    """The draft files whose bytes no longer match the sha256 recorded when the run wrote them."""
    changed = []
    for name, digest in sorted(report["drafts_sha256"].items()):
        try:
            data = (pathlib.Path(folder) / name).read_bytes()
        except OSError:
            changed.append(f"{name} (missing)")
            continue
        if sha256(data) != digest:
            changed.append(name)
    return changed


def check_drafts(folder, report):
    changed = changed_drafts(folder, report)
    if changed:
        raise PipelineError("a draft was changed after it was written (its sha256 no longer matches): "
                            + ", ".join(changed) + "; to change a draft, change the input and run again", 2)


def entry_problem(entry, items):
    """Why an entry of decisions.json cannot be used, or None."""
    if not isinstance(entry, dict) or sorted(entry) != sorted(ENTRY_FIELDS):
        return "it is not a decision (exactly " + ", ".join(ENTRY_FIELDS) + ")"
    if type(entry["item"]) is not int or entry["item"] not in items:
        return f"item {entry['item']!r} is not a draft of this run"
    if entry["decision"] not in ("approved", "rejected"):
        return f"decision {entry['decision']!r} is neither approved nor rejected"
    if not isinstance(entry["by"], str) or not entry["by"].strip():
        return "by is empty"
    if not isinstance(entry["at"], str) or not TIMESTAMP.fullmatch(entry["at"]):
        return "at is not a UTC timestamp"
    if entry["decision"] == "rejected" and not (isinstance(entry["reason"], str) and entry["reason"].strip()):
        return "a rejection has no reason"
    if entry["reason"] is not None and not isinstance(entry["reason"], str):
        return "reason is not a text"
    return None


def load_decisions(folder, report):
    """decisions.json of this run, or a new, empty one when there is none yet. A file that names another run, or
    that does not hold decisions, is refused (exit 2)."""
    fresh = {"request_sha256": report["request_sha256"], "drafts_sha256": drafts_digest(report["drafts_sha256"]),
             "decisions": []}
    path = pathlib.Path(folder) / DECISIONS
    if not path.exists():
        return fresh
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read {DECISIONS}: {exc}", 2)
    if not isinstance(data, dict) or sorted(data) != sorted(fresh) or not isinstance(data["decisions"], list):
        raise PipelineError(f"{DECISIONS} is not a decisions file", 2)
    for field in ("request_sha256", "drafts_sha256"):
        if data[field] != fresh[field]:
            raise PipelineError(f"{DECISIONS} names another run: its {field} is not this run's", 2)
    items = {draft["item"] for draft in report["drafts"]}
    for number, entry in enumerate(data["decisions"], start=1):
        problem = entry_problem(entry, items)
        if problem:
            raise PipelineError(f"{DECISIONS}, decision {number}: {problem}", 2)
    return data


def states(report, decisions):
    """item -> the last decision entry for it, or None while it is pending."""
    last = {draft["item"]: None for draft in report["drafts"]}
    for entry in decisions["decisions"]:
        last[entry["item"]] = entry
    return last


def state_text(entry):
    if entry is None:
        return "pending"
    text = f"{entry['decision']} by {entry['by']} at {entry['at']}"
    return text + f": {entry['reason']}" if entry["decision"] == "rejected" else text


def write_decisions(folder, decisions):
    """Write decisions.json through a temporary file beside it, so that a stopped write never leaves half a file."""
    tmp = pathlib.Path(folder) / (DECISIONS + ".tmp")
    pipeline.write_json(tmp, decisions)
    os.replace(tmp, pathlib.Path(folder) / DECISIONS)


def record(folder, item, decision, by, reason=None, at=None):
    """Append one decision on one item and return it. Refused (exit 2): not a run folder; decisions.json names another
    run; a draft was changed after it was written; the run was exported already; no such item; an empty --by; a
    rejection without a reason; a timestamp that is not UTC ISO 8601."""
    folder = pathlib.Path(folder)
    report = load_run(folder)
    decisions = load_decisions(folder, report)
    check_drafts(folder, report)
    if (folder / APPROVED).exists():
        raise PipelineError(f"this run was exported already ({APPROVED}/ exists); a later decision would not be "
                            "exported", 2)
    items = sorted(draft["item"] for draft in report["drafts"])
    if item not in items:
        raise PipelineError(f"this run has no draft with item {item} (its items: "
                            f"{', '.join(str(i) for i in items) or 'none'})", 2)
    if (by or "").startswith(pipeline.FORMULA_START) or (by or "").strip().startswith(pipeline.FORMULA_START):
        raise PipelineError("--by may not start with =, +, -, @, a tab or a carriage return: a spreadsheet could run "
                            "it as a formula", 2)
    by = (by or "").strip()
    if not by:
        raise PipelineError("--by must name the person who decides", 2)
    reason = (reason or "").strip() or None
    if decision == "rejected" and reason is None:
        raise PipelineError("a rejection needs a reason", 2)
    entry = {"item": item, "decision": decision, "by": by, "at": utc_now() if at is None else check_timestamp(at),
             "reason": reason}
    decisions["decisions"].append(entry)
    write_decisions(folder, decisions)
    return entry


def call_list_rows(folder):
    """The header and the rows of the run's call-list.csv, the rows by priority."""
    with (pathlib.Path(folder) / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, {int(row[0]): row for row in reader}


def append_history(path, lines):
    """Append lines to the run history, creating it when it is missing."""
    path = pathlib.Path(path)
    existing = path.read_bytes() if path.exists() else b""
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith(b"\n"):
            f.write("\n")
        f.write("".join(line + "\n" for line in lines))


def export(folder, history=None, as_of=None):
    """Write approved/ with only the approved drafts, and append them to the run history. Returns a summary. Refused
    (exit 2), with nothing written: approved/ exists; decisions.json names another run; a draft was changed; the
    history file is corrupt or its folder does not exist."""
    folder = pathlib.Path(folder)
    report = load_run(folder)
    target = folder / APPROVED
    if target.exists():
        raise PipelineError(f"{APPROVED}/ already exists: this run was exported before; nothing was written", 2)
    decisions = load_decisions(folder, report)
    check_drafts(folder, report)
    as_of = as_of or datetime.date.today()
    if history is not None:
        if not pathlib.Path(history).parent.is_dir():
            raise PipelineError(f"the folder of the history file does not exist: {history}; nothing was written", 2)
        pipeline.read_history(history, as_of)  # a corrupt history refuses here, before anything is written
    last = states(report, decisions)
    summary = {kind: [d["item"] for d in report["drafts"] if (last[d["item"]] or {}).get("decision") == kind]
               for kind in ("approved", "rejected")}
    summary["pending"] = [d["item"] for d in report["drafts"] if last[d["item"]] is None]
    summary["history_lines"] = 0
    approved = [d for d in sorted(report["drafts"], key=lambda d: d["item"]) if d["item"] in summary["approved"]]
    if not approved:
        return summary
    header, rows = call_list_rows(folder)
    # Built in a temporary folder and renamed, so that approved/ is either complete or absent.
    work = pathlib.Path(tempfile.mkdtemp(prefix=".approved-", dir=folder))
    (work / "reminders").mkdir()
    with (work / "call-list.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header + ["approved_by", "approved_at"])
        for draft in approved:
            w.writerow(pipeline.safe_cells(rows[draft["item"]] + [last[draft["item"]]["by"],
                                                                  last[draft["item"]]["at"]])[0])
    for draft in approved:
        shutil.copyfile(folder / draft["file"], work / draft["file"])
    work.rename(target)
    if history is not None:
        append_history(history, [json.dumps({"customer_key": d["customer_key"], "exported_on": as_of.isoformat(),
                                             "total": d["amount"], "request_sha256": report["request_sha256"]})
                                 for d in approved])
        summary["history_lines"] = len(approved)
    return summary


def listing(folder):
    """One line per draft with its state, then the counts; a changed draft and an export are named at the end."""
    folder = pathlib.Path(folder)
    report = load_run(folder)
    last = states(report, load_decisions(folder, report))
    try:
        names = {item: row[1] for item, row in call_list_rows(folder)[1].items()}
    except (OSError, ValueError, IndexError, StopIteration, csv.Error):
        names = {}
    lines = [f"item {d['item']} | {names.get(d['item'], d['customer'])} | {d['amount']} | {d['bucket']} | "
             f"{state_text(last[d['item']])}" for d in sorted(report["drafts"], key=lambda d: d["item"])]
    counts = {kind: sum(1 for e in last.values() if e and e["decision"] == kind) for kind in ("approved", "rejected")}
    pending = sum(1 for e in last.values() if e is None)
    lines.append(f"{len(last)} draft(s): {counts['approved']} approved, {counts['rejected']} rejected, "
                 f"{pending} pending")
    changed = changed_drafts(folder, report)
    if changed:
        lines.append("changed after it was written, so it cannot be decided or exported: " + ", ".join(changed))
    if (folder / APPROVED).exists():
        lines.append(f"exported: {APPROVED}/ exists")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="each draft with its state: pending, approved or rejected")
    p.add_argument("run", help="the run folder")
    for name, what in (("approve", "record an approval"), ("reject", "record a rejection, with its reason")):
        p = sub.add_parser(name, help=what)
        p.add_argument("run", help="the run folder")
        p.add_argument("--item", type=int, required=True, help="the draft's priority in the call list")
        p.add_argument("--by", required=True, help="who decides: recorded as given, not authenticated")
        p.add_argument("--at", help="a UTC timestamp, yyyy-mm-ddThh:mm:ssZ (default: now)")
        if name == "reject":
            p.add_argument("--reason", required=True)
    p = sub.add_parser("export", help="write approved/ and add the approved customers to the run history")
    p.add_argument("run", help="the run folder")
    p.add_argument("--history", help="the run history to append to (JSON lines; created when missing)")
    p.add_argument("--as-of", type=pipeline.iso_date, help="the export's date, yyyy-mm-dd (default: today)")
    args = ap.parse_args(argv)
    try:
        if args.command == "list":
            print("\n".join(listing(args.run)))
        elif args.command in ("approve", "reject"):
            decision = "approved" if args.command == "approve" else "rejected"
            entry = record(args.run, args.item, decision, args.by, getattr(args, "reason", None), args.at)
            print(f"item {entry['item']}: {state_text(entry)}")
        else:
            summary = export(args.run, args.history, args.as_of)
            done = (f"exported items {', '.join(str(i) for i in summary['approved'])} to {APPROVED}/"
                    if summary["approved"] else "nothing approved: nothing exported")
            line = f"{done} | rejected {len(summary['rejected'])} | pending {len(summary['pending'])}"
            if args.history is not None:
                line += f" | history lines added {summary['history_lines']}"
            print(line + " | sent to customers 0")
    except PipelineError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
