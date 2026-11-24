"""The failure gallery: each named case runs the real command lines, then prints what was expected, what happened, and
PASS or FAIL.

  python -I demo.py --out <folder>                  every case, in a new folder demo-NNN under <folder>
  python -I demo.py --case <name> --out <folder>    one case
  python -I demo.py --list                          the cases and what each shows

The wrong answers and the broken sheets are in gallery/. They are constructed, never recorded: each is the recorded
answer or the shipped sheet with one thing changed, and gallery/README.md says what. The API case talks to a mock
server on 127.0.0.1 that answers 500; the webhook case posts to a closed port on 127.0.0.1. Nothing leaves this
computer, and nothing is sent to any customer.

Standard library only. Exit codes: 0 every case ended as expected; 1 a case did not; 2 refused (usage).
"""
import argparse
import contextlib
import csv
import datetime
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import socket
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402

SHEET = HERE / "customers.synthetic.csv"
PROMPT = HERE / "prompt.txt"
RECORDED = HERE / "recorded-answer.json"
GALLERY = HERE / "gallery"
BY = "Reviewer One (invented)"
REJECT_REASON = "Hold until the note for a person about this customer is dealt with"


def invoke(module, argv):
    """Run a command line in this process and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def first_line(text):
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def run_report(folder):
    return json.loads((folder / "run-report.json").read_text(encoding="utf-8"))


def written(folder):
    return "a run folder was written" if folder.exists() else "nothing written"


class Settings:
    """The run date, and the times of the decisions: a minute apart from --at when it is given, so that a run can be
    reproduced; the real time of each decision otherwise."""

    def __init__(self, as_of, at):
        self.as_of, self.at, self.decisions = as_of, at, 0

    def at_args(self):
        if self.at is None:
            return []
        stamp = self.at + datetime.timedelta(minutes=self.decisions)
        self.decisions += 1
        return ["--at", stamp.strftime(approval.TIMESTAMP_FORMAT)]


def pipeline_run(out, settings, *extra, sheet=SHEET, answer=RECORDED):
    return invoke(pipeline, ["--sheet", sheet, "--recorded", answer, "--out", out, "--as-of", settings.as_of, *extra])


def decide(run, settings, item, reason=None):
    argv = ["approve" if reason is None else "reject", run, "--item", item, "--by", BY]
    return invoke(approval, argv + (["--reason", reason] if reason else []) + settings.at_args())


def baseline():
    """What the recorded answer gives on the shipped sheet, checked in memory: the number of accepted items and the
    reasons of the rejected ones. A constructed case is judged against it, by the one thing it changed."""
    prep = pipeline.prepare(SHEET, PROMPT)
    accepted, rejected, *_ = pipeline.validate(pipeline.parse_answer(RECORDED.read_text(encoding="utf-8")),
                                               prep["expected"], prep["key"])
    return len(accepted), [r["reason"] for r in rejected]


def one_more_rejection(data, before, words):
    """The rejections that hold these words, when there is exactly one more of them than before; else []."""
    found = [r for r in data["rejected"] if words in r["reason"]]
    same = len(found) == len([reason for reason in before if words in reason]) + 1
    return found if same and len(data["rejected"]) == len(before) + 1 else []


def flagged(data):
    """The drafts' items whose customer a note for a person names, and the others, each in priority order."""
    def named(who):
        return any(re.search(r"(?<!\w)" + re.escape(who) + r"(?!\w)", note) for note in data["for_a_human"])
    drafts = sorted(data["drafts"], key=lambda d: d["item"])
    return [d["item"] for d in drafts if named(d["customer"])], [d["item"] for d in drafts if not named(d["customer"])]


def happy_path(folder, settings):
    """A person approves the first two drafts that the model raised no note about, and rejects the first draft that
    a note names, as a person would hold a reminder until the note is dealt with."""
    run, history = folder / "run", folder / "history.jsonl"
    code, _, err = pipeline_run(run, settings)
    if code != 0:
        return False, f"the pipeline ended with exit {code}: {first_line(err)}"
    data = run_report(run)
    noted, clear = flagged(data)
    if len(clear) < 2 or not noted:
        return False, (f"the run has {len(clear)} draft(s) without a note and {len(noted)} with one; this case needs "
                       "two and one")
    approve, reject = clear[:2], noted[0]
    for item, reason in [(approve[0], None), (approve[1], None), (reject, REJECT_REASON)]:
        code, _, err = decide(run, settings, item, reason)
        if code != 0:
            return False, f"a decision on item {item} was refused: {first_line(err)}"
    code, _, err = invoke(approval, ["export", run, "--history", history, "--as-of", settings.as_of])
    if code != 0:
        return False, f"the export ended with exit {code}: {first_line(err)}"
    code, _, err = invoke(report, [run, "--out", folder / "report.html"])
    if code != 0:
        return False, f"the report ended with exit {code}: {first_line(err)}"
    exported = sorted(p.name for p in (run / "approved" / "reminders").iterdir())
    with (run / "approved" / "call-list.csv").open(encoding="utf-8-sig", newline="") as f:
        lines = [int(row[0]) for row in list(csv.reader(f))[1:]]
    history_lines = len(history.read_text(encoding="utf-8").splitlines())
    ok = (exported == [f"reminder-{i:02d}.txt" for i in approve] and lines == approve and history_lines == 2)
    return ok, (f"exit 0; {len(data['drafts'])} drafts; items {approve[0]} and {approve[1]} approved, item {reject} "
                f"rejected (a note names it), {len(data['drafts']) - 3} pending; the export holds reminders "
                f"{', '.join(exported)} and call-list lines {', '.join(str(i) for i in lines)}; history lines "
                f"{history_lines}; report written")


def bad_sheet(folder, settings):
    run = folder / "run"
    code, _, err = pipeline_run(run, settings, sheet=GALLERY / "bad-sheet.csv")
    ok = code == 2 and not run.exists() and "amount_due '4,200'" in err
    return ok, f"exit {code}; {written(run)}; {first_line(err)}"


def wrong_total(folder, settings):
    run = folder / "run"
    accepted, rejected = baseline()
    code, _, err = pipeline_run(run, settings, answer=GALLERY / "wrong-total.json")
    if code != 0:
        return False, f"exit {code}: {first_line(err)}"
    data = run_report(run)
    new = one_more_rejection(data, rejected, "does not match the sheet's total")
    ok = bool(new) and data["accepted"] == accepted - 1
    shown = f"{new[-1]['customer']} ({new[-1]['reason']})" if new else "no new rejection"
    return ok, f"exit 0; rejected {shown}; the other {data['accepted']} items kept"


def invented_customer(folder, settings):
    run = folder / "run"
    accepted, rejected = baseline()
    code, _, err = pipeline_run(run, settings, answer=GALLERY / "invented-customer.json")
    if code != 0:
        return False, f"exit {code}: {first_line(err)}"
    data = run_report(run)
    new = one_more_rejection(data, rejected, "this customer is not in the sheet")
    ok = bool(new) and data["accepted"] == accepted
    shown = f"{new[-1]['customer']} ({new[-1]['reason']})" if new else "no new rejection"
    return ok, f"exit 0; rejected {shown}; the other {data['accepted']} items kept"


def leak_guard(folder, settings):
    run = folder / "run"
    code, _, err = pipeline_run(run, settings, sheet=GALLERY / "leak-sheet.csv")
    ok = code == 2 and not run.exists() and "would reach the model; nothing was sent" in err
    return ok, f"exit {code}; {written(run)}; {first_line(err)}"


def second_run(folder, settings):
    first, second, history = folder / "first", folder / "second", folder / "history.jsonl"
    code, _, err = pipeline_run(first, settings)
    if code != 0:
        return False, f"the first run ended with exit {code}: {first_line(err)}"
    for item in sorted(d["item"] for d in run_report(first)["drafts"]):
        code, _, err = decide(first, settings, item)
        if code != 0:
            return False, f"a decision on item {item} was refused: {first_line(err)}"
    code, _, err = invoke(approval, ["export", first, "--history", history, "--as-of", settings.as_of])
    if code != 0:
        return False, f"the export ended with exit {code}: {first_line(err)}"
    code, _, err = pipeline_run(second, settings, "--history", history)
    if code != 0:
        return False, f"the second run ended with exit {code}: {first_line(err)}"
    data = run_report(second)
    ok = (not data["model_called"] and len(data["already_drafted"]) == data["customers_owing"]
          and data["drafts"] == [])
    return ok, (f"exit 0; the second run found {len(data['already_drafted'])} of {data['customers_owing']} owing "
                f"customers already drafted; model called: {'yes' if data['model_called'] else 'no'}; "
                f"drafts written: {len(data['drafts'])}")


def no_approval(folder, settings):
    run, history = folder / "run", folder / "history.jsonl"
    code, _, err = pipeline_run(run, settings)
    if code != 0:
        return False, f"the pipeline ended with exit {code}: {first_line(err)}"
    code, printed, err = invoke(approval, ["export", run, "--history", history, "--as-of", settings.as_of])
    exported = (run / "approved").exists() or history.exists()
    ok = code == 0 and not exported and printed.startswith("nothing approved: nothing exported")
    return ok, (f"exit {code}; {first_line(printed or err).split(' | ')[0]}; approved/ or history written: "
                f"{'yes' if exported else 'no'}")


def foreign_decisions(folder, settings):
    first, second = folder / "first", folder / "second"
    for run, answer in [(first, RECORDED), (second, GALLERY / "wrong-total.json")]:
        code, _, err = pipeline_run(run, settings, answer=answer)
        if code != 0:
            return False, f"a run ended with exit {code}: {first_line(err)}"
    code, _, err = decide(first, settings, 1)
    if code != 0:
        return False, f"the decision was refused: {first_line(err)}"
    shutil.copyfile(first / approval.DECISIONS, second / approval.DECISIONS)
    code, _, err = invoke(approval, ["export", second, "--as-of", settings.as_of])
    exported = (second / "approved").exists()
    ok = code == 2 and not exported and "names another run" in err
    return ok, f"exit {code}; approved/ written: {'yes' if exported else 'no'}; {first_line(err)}"


class Answers500(http.server.BaseHTTPRequestHandler):
    """A stand-in for the model API that fails every request with HTTP 500."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = b'{"type": "error", "error": {"type": "api_error", "message": "a local mock that always fails"}}'
        self.send_response(500)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def placeholder_key():
    """The API path needs a key in the environment. The mock never checks it, so a placeholder stands in for the
    duration of the case, and whatever was there before is put back."""
    old = os.environ.get(pipeline.API_KEY_ENV)
    os.environ[pipeline.API_KEY_ENV] = "demo" + "-" + "placeholder"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(pipeline.API_KEY_ENV, None)
        else:
            os.environ[pipeline.API_KEY_ENV] = old


def api_error(folder, settings):
    run = folder / "run"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answers500)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        with placeholder_key():
            code, _, err = pipeline_run(run, settings, "--backend", "anthropic", "--api-base",
                                        f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
    ok = code == 3 and not run.exists() and "HTTP status 500" in err
    return ok, f"exit {code}; {written(run)}; {first_line(err)}"


def webhook_down(folder, settings):
    run = folder / "run"
    with socket.socket() as s:  # a local port where nothing listens
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code, printed, err = pipeline_run(run, settings, "--webhook", f"http://127.0.0.1:{port}/webhook")
    kept = all((run / name).is_file() for name in ("call-list.csv", "for-a-person.txt", "run-report.json"))
    status = run_report(run).get("webhook_status") if kept else None
    ok = code == 4 and kept and str(status).startswith("failed")
    return ok, f"exit {code}; outputs kept: {'yes' if kept else 'no'}; webhook {'failed' if ok else status}"


CASES = [
    ("happy-path", "The recorded answer, approvals for some drafts, the export, the report",
     "exit 0; the export holds only the approved drafts", happy_path),
    ("bad-sheet", "An amount written 4,200", "refused before anything is sent, exit 2, nothing written", bad_sheet),
    ("wrong-total", "The model's total differs from the sheet's", "that item rejected with the reason; the others kept",
     wrong_total),
    ("invented-customer", "The model adds a customer who is not in the sheet", "rejected", invented_customer),
    ("leak-guard", "A personal value still in the request", "refused, exit 2, nothing sent", leak_guard),
    ("second-run", "The same sheet again inside the window, after an export",
     "nothing is drafted twice; the model is not called", second_run),
    ("no-approval", "An export with no decision", "nothing exported", no_approval),
    ("foreign-decisions", "A decisions file from another run", "refused, exit 2", foreign_decisions),
    ("api-error", "The live path against a local mock that answers 500", "refused, exit 3, nothing written",
     api_error),
    ("webhook-down", "The webhook is unreachable", "outputs kept, exit 4", webhook_down),
]


def new_folder(base):
    """A new folder demo-NNN under base, the first number not taken."""
    base.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        folder = base / f"demo-{number:03d}"
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            number += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="a base folder; each run of the demo writes a new folder demo-NNN under it")
    ap.add_argument("--case", choices=[case[0] for case in CASES], help="run this case only")
    ap.add_argument("--list", action="store_true", help="list the cases and stop")
    ap.add_argument("--as-of", type=pipeline.iso_date, help="the runs' date, yyyy-mm-dd (default: today)")
    ap.add_argument("--at", help="the time of the first decision, yyyy-mm-ddThh:mm:ssZ (UTC); each next decision "
                                 "a minute later (default: the real time of each)")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(f"{name}: {shows}" for name, shows, _, _ in CASES))
        return 0
    if not args.out:
        ap.error("--out is required, except with --list")
    try:
        at = None if args.at is None else datetime.datetime.strptime(approval.check_timestamp(args.at),
                                                                     approval.TIMESTAMP_FORMAT)
    except pipeline.PipelineError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    settings = Settings((args.as_of or datetime.date.today()).isoformat(), at)
    folder = new_folder(pathlib.Path(args.out))
    cases = [case for case in CASES if args.case in (None, case[0])]
    passed = 0
    for name, _, expected, run_case in cases:
        case_folder = folder / name
        case_folder.mkdir()
        try:
            ok, happened = run_case(case_folder, settings)
        except Exception as exc:  # a case that breaks is reported as a FAIL with its reason, and the others still run
            ok, happened = False, f"the case stopped: {type(exc).__name__}: {exc}"
        passed += ok
        print(f"{name} | expected: {expected} | happened: {happened} | {'PASS' if ok else 'FAIL'}")
    print(f"{passed} of {len(cases)} case(s) ended as expected | written under {folder.as_posix()}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
