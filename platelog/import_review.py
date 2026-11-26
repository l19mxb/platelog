"""Administrator review of registrations skipped during retained imports."""

import csv
from datetime import datetime
from html import escape
from io import StringIO
from urllib.parse import urlencode

from .daily_csv import csv_text
from .admin_ui import field
from .store import IMPORT_HISTORY_LIMIT, Store


def review_url(run_id: str, page: int = 1) -> str:
    return "/admin/import-review?" + urlencode({"run": run_id, "page": page})


def review_content(store: Store, run_id: str | None, number: int, zone) -> str:
    history = store.registration_import_history()
    intro = ('<div class="import-review"><p>Fix the source file and import it again. This view lists invalid or empty authorised registrations. '
             'Non-authorised No rows are excluded from registration matching.</p>')
    if not history:
        return intro + ('<p>No import history yet. Detailed records are saved for imports run after this update.</p></div>')
    run_id = run_id or history[0]["id"]
    run, rows, number, pages = store.registration_import_page(run_id, number)

    def when(value):
        return datetime.fromtimestamp(value / 1000, zone).strftime("%d/%m/%Y %H:%M:%S %Z")

    options = []
    for item in history:
        filename = item["source"].replace("\\", "/").rsplit("/", 1)[-1]
        count = item["summary"]["import_report"].get("skipped_entries", 0)
        label = f'{when(item["at_ms"])} · {filename[:80]} · {count:,} skipped'
        if item["error"]:
            label += " · Failed"
        options.append(f'<option value="{item["id"]}" {"selected" if item["id"] == run_id else ""}>{escape(label)}</option>')
    run_field = field("review-run", "Import run", f'<select name="run">{"".join(options)}</select>',
                      "Includes manual uploads and scheduled refreshes. The latest 30 import runs retain every skipped entry.", wide=True)
    selection = ('<form class="form-grid" method="get" action="/admin/import-review">'
                 f'{run_field}<div class="form-actions"><button type="submit">View import</button></div></form>')
    report = run["summary"]["import_report"]
    skipped = report.get("skipped_entries", 0)
    source_rows = report.get("skipped_rows", 0)
    kind = "Scheduled refresh" if run["kind"] == "scheduled" else "Manual upload"
    status = "Failed. The registered list was kept." if run["error"] else "Applied"
    details = (f'<section><h3>Import details</h3><dl><dt>Source</dt><dd>{escape(run["source"])}</dd>'
               f'<dt>Time</dt><dd>{when(run["at_ms"])}</dd><dt>Type</dt><dd>{kind}</dd>'
               f'<dt>Result</dt><dd>{status}</dd></dl>'
               f'<p>{skipped:,} skipped entries across {source_rows:,} source rows.</p>')
    if run["error"]:
        details += f'<p class="notice">{escape(run["error"])}</p>'
    if not rows:
        details += '<p>No individual skipped entries were recorded for this import.</p>'
    else:
        details += (f'<p><a class="camera-add" href="/admin/import-review/{run_id}/skipped.csv">'
                    'Download skipped rows CSV</a></p>')
        body = []
        for row in rows:
            def text(value):
                return escape(value) if value.strip() else '<span class="muted">(blank)</span>'

            vehicle_ending = "…" if row["vehicle_shortened"] else ""
            entry_ending = "…" if row["entry_shortened"] else ""
            body.append(f'<tr><td>{row["source_row"]}</td><td>{row["entry_number"]}</td>'
                        f'<td class="review-value">{text(row["vehicle"])}{vehicle_ending}</td>'
                        f'<td class="review-value">{text(row["entry"])}{entry_ending}</td>'
                        f'<td>{escape(row["reason"])}</td></tr>')
        details += ('<div class="review-table"><table><thead><tr><th>Source row</th><th>Entry</th>'
                    '<th>Original Vehicle value</th><th>Skipped entry</th><th>Reason</th></tr></thead>'
                    f'<tbody>{"".join(body)}</tbody></table></div>')
        if any(row["vehicle_shortened"] or row["entry_shortened"] for row in rows):
            details += '<p>Long values are shortened to 240 characters here. The CSV contains the full values.</p>'
        previous = (f'<a href="{escape(review_url(run_id, number - 1))}">Previous</a>' if number > 1 else "")
        following = (f'<a href="{escape(review_url(run_id, number + 1))}">Next</a>' if number < pages else "")
        details += f'<nav class="pagination" aria-label="Skipped entries pages">{previous}<span>Page {number} of {pages}</span>{following}</nav>'
    return (intro + selection + details + '</section>'
            f'<p class="muted">The latest {IMPORT_HISTORY_LIMIT} import runs are retained. All skipped entries in those runs are available.</p></div>')


def skipped_csv(store: Store, run_id: str) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Source row", "Original Vehicle value", "Skipped entries", "Reasons"))
    for row in store.registration_skipped_rows(run_id):
        writer.writerow((row["source_row"], csv_text(row["vehicle"]), csv_text(row["entries"]), csv_text(row["reasons"])))
    return output.getvalue().encode("utf-8-sig")
