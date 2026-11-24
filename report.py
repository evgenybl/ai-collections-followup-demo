"""Write one self-contained HTML page about a run, for a person to read: what is owed, what was drafted, what the
checks refused and what was decided.

The page has no script, no external file and no font download: plain HTML with a small stylesheet inside it. It reads
on a phone and prints cleanly. Every value on it comes from the run folder's own files (run-report.json,
call-list.csv, the reminders, for-a-person.json and decisions.json), and every text from the model or the sheet is
escaped. Like for-a-person.txt it holds real names, and it says so at the top. Write it again after decisions: it
shows the decisions recorded when it is written.

Standard library only. Run: python -I report.py <run folder> --out <file.html>
Exit codes: 0 written; 2 refused (not a run folder, decisions from another run, a draft changed after it was written).
"""
import argparse
import html
import json
import pathlib
import sys
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import approval  # noqa: E402
import pipeline  # noqa: E402
from pipeline import PipelineError  # noqa: E402

STYLE = """
:root { --ink: #1f2933; --muted: #52606d; --line: #d9e2ec; --soft: #f5f7fa; --bar: #486581; --bar-edge: #243b53;
  --approved: #1f6f43; --rejected: #9b2c2c; --pending: #7a5b00; }
* { box-sizing: border-box; }
body { margin: 0; background: #ffffff; color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 64rem; margin: 0 auto; padding: 1rem; }
h1 { font-size: 1.5rem; line-height: 1.25; margin: .5rem 0; }
h2 { font-size: 1.2rem; margin: 2rem 0 .75rem; padding-bottom: .25rem; border-bottom: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 1rem 0 .25rem; }
.meta { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0; color: var(--muted); }
.tag { border: 1px solid var(--line); border-radius: 1rem; padding: 0 .6rem; background: var(--soft); }
.warning { border-left: 4px solid var(--pending); padding: .5rem .75rem; background: var(--soft); }
.figures { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem; }
.figure { border: 1px solid var(--line); border-radius: .5rem; padding: .75rem; }
.figure span { display: block; color: var(--muted); font-size: .9rem; }
.figure b { display: block; font-size: 1.6rem; font-variant-numeric: tabular-nums; }
.bar-row { display: grid; grid-template-columns: 5rem 1fr 13rem; gap: .75rem; align-items: center; margin: .4rem 0; }
.track { display: block; height: 1.25rem; background: var(--soft); border: 1px solid var(--line); }
.bar { display: block; height: 100%; min-width: 1px; background: var(--bar); border-right: 2px solid var(--bar-edge);
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.value { font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; vertical-align: top; padding: .45rem .5rem; border-bottom: 1px solid var(--line); }
th { font-size: .85rem; color: var(--muted); }
td.number { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.state { font-weight: 600; }
.approved { color: var(--approved); } .rejected { color: var(--rejected); } .pending { color: var(--pending); }
.changed { display: block; font-size: .85rem; color: var(--pending); }
.draft { border: 1px solid var(--line); border-radius: .5rem; padding: .5rem .75rem; margin: .75rem 0; }
.draft h3 { margin-top: .25rem; }
pre { white-space: pre-wrap; font: inherit; margin: .5rem 0; padding: .5rem; background: var(--soft); }
footer { margin-top: 2.5rem; padding-top: .75rem; border-top: 1px solid var(--line); color: var(--muted); }
footer p { margin: .25rem 0; }
.sent { color: var(--ink); font-weight: 700; }
@media (max-width: 40rem) {
  .bar-row { grid-template-columns: 4.5rem 1fr; }
  .bar-row .value { grid-column: 2; }
}
@media print {
  body { font-size: 11pt; }
  main { max-width: none; padding: 0; }
  .scroll { overflow: visible; }
  .draft, .figure, .bar-row, tr { break-inside: avoid; }
  h2 { break-after: avoid; }
}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def shown(text):
    """An amount from the run's files, written for a reader: 12,800 or 15,655.45. Never rounded: an amount with more
    than two decimals is shown as written."""
    value = Decimal(text)
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    if value == value.quantize(Decimal("0.01")):
        return f"{value:,.2f}"
    return pipeline.money(value)


def backend_label(report):
    """What answered, read from the run report's facts. Recorded only when the replayed file and the request are the
    shipped recording's (its meta file's sha256s); the shipped recording replayed for another request says so; any
    other replayed file's origin is not verified. Live only for the Anthropic backend at its default address; for any
    other address, "Live path, custom endpoint" and the host it went to."""
    if not report["model_called"]:
        return "No model answer: every owing customer was already drafted"
    if report["backend"] == "recorded":
        if report.get("answer_is_shipped_recording") and report.get("request_is_shipped_recordings"):
            return "Recorded"
        if report.get("answer_is_shipped_recording"):
            return "Recorded answer, replayed for another request"
        return "Replayed file, origin not verified"
    if report["backend"] == "anthropic":
        if report.get("api_default_address"):
            return "Live"
        return f"Live path, custom endpoint {report.get('api_host') or 'not recorded'}"
    return report["backend"]


def sheet_label(report):
    """Synthetic data only when the sheet is one this repository ships (its sha256); otherwise the user's sheet."""
    return "Synthetic data" if report.get("sheet_is_shipped") else "Sheet supplied by the user"


def state_html(entry):
    if entry is None:
        return '<span class="state pending">Waiting for a decision</span>'
    text = f"{entry['decision'].capitalize()} by {esc(entry['by'])} at {esc(entry['at'])}"
    if entry["decision"] == "rejected":
        text += f": {esc(entry['reason'])}"
    return f'<span class="state {esc(entry["decision"])}">{text}</span>'


def listed(items, empty="None."):
    if not items:
        return f"<p>{esc(empty)}</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def load(folder):
    """Everything the page shows, from the run folder's own files; refused (exit 2) as approval.py refuses."""
    folder = pathlib.Path(folder)
    report = approval.load_run(folder)
    decisions = approval.load_decisions(folder, report)
    approval.check_drafts(folder, report)
    try:
        person = json.loads((folder / "for-a-person.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError("there is no for-a-person.json in the run folder; run the pipeline again", 2)
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read for-a-person.json: {exc}", 2)
    header, rows = approval.call_list_rows(folder)
    rows = {item: dict(zip(header, row)) for item, row in rows.items()}
    reminders = {d["item"]: (folder / d["file"]).read_text(encoding="utf-8") for d in report["drafts"]}
    return report, approval.states(report, decisions), person, rows, reminders


def page(report, states, person, rows, reminders):
    drafts = sorted(report["drafts"], key=lambda d: d["item"])
    total_owed = Decimal(report["total_owed"])
    names = ("in this demo every name is invented" if report.get("sheet_is_shipped")
             else "from the sheet supplied by the user")
    out = ["<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>Collections run as of {esc(report['as_of'])}</title>", f"<style>{STYLE}</style>", "</head>",
           "<body>", "<main>"]

    out += ['<header id="header">',
            f'<p class="meta"><span class="tag">{esc(sheet_label(report))}</span>'
            f'<span class="tag">{esc(backend_label(report))}</span>'
            f'<span class="tag">As of {esc(report["as_of"])}</span></p>',
            "<h1>Collections follow-up: drafts for a person to decide</h1>",
            f'<p class="warning">This page holds real names ({names}). Keep it on this computer, as you would keep '
            "for-a-person.txt.</p>", "</header>"]

    figures = [("Customers owing", report["customers_owing"]), ("Total owed", shown(report["total_owed"])),
               ("Drafts waiting for a decision", sum(1 for d in drafts if states[d["item"]] is None)),
               ("Items the checks refused", len(report["rejected"]))]
    out += ['<section id="figures" class="figures" aria-label="In figures">']
    out += [f'<div class="figure"><span>{esc(name)}</span><b>{esc(value)}</b></div>' for name, value in figures]
    out += ["</section>"]

    out += ['<section id="ageing">', "<h2>What is owed, by how late it is</h2>"]
    for bucket in report["ageing"]:
        total = Decimal(bucket["total"])
        width = total / total_owed * 100 if total_owed else Decimal(0)
        count = len(bucket["customers"])
        out.append(f'<div class="bar-row"><span>{esc(bucket["bucket"])}</span>'
                   f'<span class="track"><span class="bar" style="width: {width:.1f}%"></span></span>'
                   f'<span class="value">{shown(bucket["total"])} &middot; {count} '
                   f'customer{"" if count == 1 else "s"}</span></div>')
    out += ["<p>The bucket of each customer comes from the largest delay among their open invoices in the sheet. "
            "&ldquo;current&rdquo; means not late yet.</p>", "</section>"]

    out += ['<section id="call-list">', "<h2>Call list, in priority order</h2>"]
    if drafts:
        out += ['<div class="scroll"><table>', "<thead><tr><th>#</th><th>Customer</th><th>Total</th><th>Days late</th>"
                "<th>Bucket</th><th>Why at this place (the model's reason)</th><th>Decision</th></tr></thead>",
                "<tbody>"]
        for d in drafts:
            row = rows[d["item"]]
            changed = '<span class="changed">amount changed since the last draft</span>' if d["amount_changed"] else ""
            out.append(f'<tr><td>{d["item"]}</td><td>{esc(row["customer"])}{changed}</td>'
                       f'<td class="number">{shown(row["amount"])}</td><td class="number">{esc(row["days_late"])}</td>'
                       f'<td>{esc(row["bucket"])}</td><td>{esc(row["reason"])}</td>'
                       f'<td>{state_html(states[d["item"]])}</td></tr>')
        out += ["</tbody></table></div>"]
    else:
        out += ["<p>No drafts in this run.</p>"]
    out += ["</section>"]

    out += ['<section id="reminders">', "<h2>Reminder drafts</h2>"]
    for d in drafts:
        out.append(f'<article class="draft"><h3>Item {d["item"]}: {esc(rows[d["item"]]["customer"])}</h3>'
                   f'<p>{state_html(states[d["item"]])}</p><pre>{esc(reminders[d["item"]].rstrip())}</pre></article>')
    if not drafts:
        out.append("<p>No reminder drafts in this run.</p>")
    out += ["</section>"]

    out += ['<section id="refused">', "<h2>What the checks refused</h2>",
            "<h3>Items refused (not in the call list)</h3>",
            listed([f"<b>{esc(r['customer'])}</b>: {esc(r['reason'])}" for r in person["rejected"]]),
            "<h3>Customers who owe money and are missing from the answer</h3>",
            listed([esc(who) for who in person["missing_from_answer"]]),
            "<h3>Notes for a person that the checks refused</h3>",
            listed([f"{esc(r['note'])} <i>({esc(r['reason'])})</i>" for r in person["rejected_notes"]]),
            "</section>"]

    out += ['<section id="already-drafted">', "<h2>Already drafted</h2>"]
    if report["history_lines"] is None:
        out.append("<p>No run history was given for this run, so every owing customer was asked about.</p>")
    else:
        out += [f"<p>Exported in the {esc(report['window_days'])} days up to {esc(report['as_of'])} with the same "
                "total, so left out of the request:</p>",
                listed([f"{esc(r['customer'])}: drafted on {esc(r['drafted_on'])}, total {shown(r['total'])}"
                        for r in person["already_drafted"]]),
                "<p>Drafted again, because the amount changed since the last draft:</p>",
                listed([f"{esc(r['customer'])}: {shown(r['total'])} now, {shown(r['last_total'])} when drafted on "
                        f"{esc(r['last_drafted_on'])}" for r in person["amount_changed"]]),
                "<p>Answered although already drafted (from an answer to another request; set aside, not counted as "
                "refused):</p>",
                listed([esc(who) for who in person.get("answered_although_drafted", [])])]
    out += ["</section>"]

    out += ['<section id="notes">', "<h2>Notes for a person</h2>",
            listed([esc(note) for note in person["for_a_human"]]), "</section>"]

    sent = report["sent_to_customers"]
    out += ['<footer id="footer">',
            "<p>Checked by a script: every figure the model wrote in digits, against the sheet; every stand-in, "
            "against this run; the request, for personal values, before the model was asked.</p>",
            "<p>Not checked: a figure written in words, whether the order of the calls is wise, and the tone of a "
            "reminder. A person decides those.</p>",
            "<p>A decision records the name a person gave. Nobody was authenticated.</p>",
            '<p class="sent">' + ("Nothing was sent to any customer." if sent == 0 else
                                  f"Sent to customers: {esc(sent)}.") + "</p>",
            "</footer>", "</main>", "</body>", "</html>"]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="the run folder")
    ap.add_argument("--out", required=True, help="the HTML file to write (written again each time)")
    args = ap.parse_args(argv)
    try:
        report, states, person, rows, reminders = load(args.run)
    except PipelineError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pipeline.write_text(out, page(report, states, person, rows, reminders))
    waiting = sum(1 for entry in states.values() if entry is None)
    print(f"report written to {out.as_posix()} | customers owing {report['customers_owing']} | waiting for a "
          f"decision {waiting} | refused by the checks {len(report['rejected'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
