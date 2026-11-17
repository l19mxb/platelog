"""Render the compact, rolling 24-hour register summary."""

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .store import Store

HOUR_MS = 3_600_000
BIN_MS = HOUR_MS // 4
DAY_MS = 24 * HOUR_MS
BIN_COUNT = DAY_MS // BIN_MS


def render_stats(store: Store, end_ms: int, camera: str, zone: timezone | ZoneInfo) -> str:
    start_ms = end_ms - DAY_MS
    rows = store.passage_activity(start_ms, end_ms, camera)
    bins = [0] * BIN_COUNT
    unregistered = set()
    for capture_ms, plate, registered in rows:
        bins[(capture_ms - start_ms) // BIN_MS] += 1
        if not registered:
            unregistered.add(plate)

    def local(value: int) -> datetime:
        return datetime.fromtimestamp(value / 1_000, zone)

    peak = max(1, max(bins))
    points = " ".join(f"{index * 10 + 5},{59 - count * 58 / peak:.2f}" for index, count in enumerate(bins))
    hover = []
    for index, count in enumerate(bins):
        first = local(start_ms + index * BIN_MS)
        last = local(start_ms + (index + 1) * BIN_MS)
        label = f"{first:%d/%m %H:%M %Z}–{last:%d/%m %H:%M %Z} · {count:,} {'pass' if count == 1 else 'passes'}"
        hover.append(
            f'<rect x="{index * 10}" y="0" width="10" height="60" fill="transparent">'
            f'<title>{escape(label)}</title></rect>'
        )
    ticks = [local(start_ms + offset * HOUR_MS).strftime("%H:%M") for offset in (0, 6, 12, 18)]
    time_labels = "".join(f"<span>{value}</span>" for value in (*ticks, "Now"))
    return f'''<dl class="stats-metrics">
        <div><dt>Total vehicle passes</dt><dd data-total>{len(rows):,}</dd></div>
        <div><dt title="Distinct plates absent from the current registered list">Unregistered vehicles</dt><dd data-unregistered>{len(unregistered):,}</dd></div>
        </dl>
        <div class="stats-chart"><div class="stats-y" aria-hidden="true"><span>{peak:,}</span><span>0</span></div>
        <svg viewBox="0 0 960 60" preserveAspectRatio="none" role="img"
             aria-label="Vehicle passes in 15-minute intervals over the last 24 hours">
          <path class="stats-grid" d="M0 1H960 M0 30H960 M0 59H960"/>
          <polyline class="stats-line" points="{points}" vector-effect="non-scaling-stroke"/>
          {"".join(hover)}
        </svg><div class="stats-x" aria-hidden="true">{time_labels}</div></div>'''
