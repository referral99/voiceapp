#!/usr/bin/env python
"""Build an Excel activity report (with charts) from the activity.log file.

What it does
------------
1. Reads the activity log (default: ./activity.log) produced by
   ``activity_logging.logger.log_event`` - lines look like::

       2026-10-03 19:07:16,721 ACTIVITY action=visit ts=... user=user:1 ip=49.43.152.62 ...

2. Parses every ``ACTIVITY`` line into structured records (timestamp, action,
   user, ip, and any extra key=value fields).
3. Resolves each unique IP address to a country. Lookups are cached on disk
   (``ip_country_cache.json``) so re-runs are fast and avoid hammering the API.
4. Writes an .xlsx workbook with:
       - "Events"        : one row per log line (with country column)
       - "By Action"     : counts per action  + bar chart
       - "By Country"    : counts per country + pie chart
       - "By Day"        : events per day     + line chart
       - "By User"       : counts per user

Usage
-----
    python activity_report.py
    python activity_report.py --log activity.log --out activity_report.xlsx
    python activity_report.py --no-geo        # skip country lookups (offline)

Only third-party dependency is openpyxl (for the workbook + charts). ``requests``
is used for the online IP->country lookup but the script degrades gracefully if
it is missing or there is no network.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))

# ``ACTIVITY`` marks a structured line. Everything after it is "key=value"
# pairs where a value may be quoted if it contained spaces.
ACTIVITY_MARKER = " ACTIVITY "
LEADING_TS_RE = re.compile(r"^(?P<logts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+")
KV_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_log(path):
    """Yield a dict for every ACTIVITY line in ``path``.

    Unparseable or non-activity lines are skipped silently.
    """
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if ACTIVITY_MARKER not in line:
                continue

            log_ts = None
            m = LEADING_TS_RE.match(line)
            if m:
                log_ts = m.group("logts")

            payload = line.split(ACTIVITY_MARKER, 1)[1]
            fields = {}
            for key, value in KV_RE.findall(payload):
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                fields[key] = value

            if not fields:
                continue

            # Prefer the structured ts= field; fall back to the log prefix.
            ts = fields.get("ts")
            dt = _parse_dt(ts) or _parse_dt(log_ts, fmt="%Y-%m-%d %H:%M:%S,%f")

            records.append(
                {
                    "datetime": dt,
                    "date": dt.date().isoformat() if dt else "",
                    "time": dt.strftime("%H:%M:%S") if dt else "",
                    "action": fields.get("action", ""),
                    "user": fields.get("user", ""),
                    "ip": fields.get("ip", ""),
                    "extra": {
                        k: v
                        for k, v in fields.items()
                        if k not in {"action", "ts", "user", "ip"}
                    },
                }
            )
    return records


def _parse_dt(value, fmt=None):
    if not value or value == "-":
        return None
    if fmt:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            return None
    # ISO 8601 with timezone, e.g. 2026-10-03T19:07:16.721038+00:00
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# IP -> country resolution
# --------------------------------------------------------------------------- #
def _special_country(ip):
    """Return a label for IPs that have no public geolocation."""
    if not ip or ip == "-":
        return "Unknown"
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return "Invalid"
    if obj.is_loopback:
        return "Localhost"
    if obj.is_private:
        return "Private network"
    if obj.is_link_local or obj.is_reserved or obj.is_unspecified:
        return "Reserved"
    return None  # publicly routable - look it up


def resolve_countries(ips, use_geo=True, cache_path=None):
    """Map every ip in ``ips`` to a country name.

    Results are cached to ``cache_path`` as JSON. When ``use_geo`` is False (or
    the lookup fails) publicly routable IPs are reported as "Unresolved".
    """
    cache_path = cache_path or os.path.join(HERE, "ip_country_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}

    result = {}
    dirty = False
    for ip in ips:
        special = _special_country(ip)
        if special is not None:
            result[ip] = special
            continue

        if ip in cache:
            result[ip] = cache[ip]
            continue

        if not use_geo:
            result[ip] = "Unresolved"
            continue

        country = _lookup_country(ip)
        if country:
            cache[ip] = country
            dirty = True
            result[ip] = country
        else:
            result[ip] = "Unresolved"

    if dirty:
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2, sort_keys=True)
        except OSError:
            pass

    return result


def _lookup_country(ip):
    """Query a free geolocation API. Returns a country name or None."""
    try:
        import requests
    except ImportError:
        return None

    # ip-api.com: free, no key, supports IPv4 + IPv6, ~45 req/min.
    url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code == 429:  # rate limited - back off
                time.sleep(2 * (attempt + 1))
                continue
            data = resp.json()
            if data.get("status") == "success":
                name = data.get("country") or "Unknown"
                code = data.get("countryCode")
                return f"{name} ({code})" if code else name
            return None
        except Exception:  # noqa: BLE001 - network best-effort
            time.sleep(1)
    return None


# --------------------------------------------------------------------------- #
# Excel report
# --------------------------------------------------------------------------- #
HEADER_FILL = PatternFill("solid", fgColor="305496")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=13)


def _style_header(ws, ncols, row=1):
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")


def _autosize(ws):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            col = cell.column_letter
            widths[col] = max(widths.get(col, 10), len(str(cell.value)) + 2)
    for col, width in widths.items():
        ws.column_dimensions[col].width = min(width, 60)


def _write_count_sheet(wb, title, header, counter, chart_kind):
    ws = wb.create_sheet(title)
    ws.append(header)
    _style_header(ws, 2)
    for key, count in counter.most_common():
        ws.append([key, count])

    n = len(counter)
    if n == 0:
        return ws

    data = Reference(ws, min_col=2, min_row=1, max_row=n + 1)
    cats = Reference(ws, min_col=1, min_row=2, max_row=n + 1)

    if chart_kind == "bar":
        chart = BarChart()
        chart.type = "col"
    elif chart_kind == "pie":
        chart = PieChart()
    elif chart_kind == "line":
        chart = LineChart()
    else:
        chart = BarChart()

    chart.title = title
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.height = 9
    chart.width = 18
    if chart_kind == "pie":
        from openpyxl.chart.label import DataLabelList

        chart.dataLabels = DataLabelList()
        chart.dataLabels.showPercent = True
    ws.add_chart(chart, "D2")
    _autosize(ws)
    return ws


def build_report(records, ip_country, out_path):
    wb = Workbook()

    # ----- Summary ------------------------------------------------------- #
    summary = wb.active
    summary.title = "Summary"
    summary["A1"] = "Activity Log Report"
    summary["A1"].font = TITLE_FONT

    actions = Counter(r["action"] for r in records if r["action"])
    users = Counter(r["user"] for r in records if r["user"])
    countries = Counter(ip_country.get(r["ip"], "Unknown") for r in records)
    per_day = Counter(r["date"] for r in records if r["date"])
    unique_ips = {r["ip"] for r in records if r["ip"]}

    dts = [r["datetime"] for r in records if r["datetime"]]
    span = ""
    if dts:
        span = f"{min(dts).isoformat()}  ->  {max(dts).isoformat()}"

    rows = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Total events", len(records)),
        ("Unique actions", len(actions)),
        ("Unique users", len(users)),
        ("Unique IPs", len(unique_ips)),
        ("Unique countries", len(countries)),
        ("Time span", span),
    ]
    for i, (label, value) in enumerate(rows, start=3):
        summary.cell(row=i, column=1, value=label).font = Font(bold=True)
        summary.cell(row=i, column=2, value=value)
    summary.column_dimensions["A"].width = 20
    summary.column_dimensions["B"].width = 50

    # ----- Events (raw) -------------------------------------------------- #
    ev = wb.create_sheet("Events")
    headers = ["Date", "Time", "Action", "User", "IP", "Country", "Details"]
    ev.append(headers)
    _style_header(ev, len(headers))
    for r in records:
        details = " ".join(f"{k}={v}" for k, v in r["extra"].items())
        ev.append(
            [
                r["date"],
                r["time"],
                r["action"],
                r["user"],
                r["ip"],
                ip_country.get(r["ip"], "Unknown"),
                details,
            ]
        )
    ev.freeze_panes = "A2"
    _autosize(ev)

    # ----- Aggregations + charts ---------------------------------------- #
    _write_count_sheet(wb, "By Action", ["Action", "Count"], actions, "bar")
    _write_count_sheet(wb, "By Country", ["Country", "Count"], countries, "pie")
    _write_count_sheet(wb, "By User", ["User", "Count"], users, "bar")

    # Day sheet needs chronological order for a sensible line chart.
    day_ws = wb.create_sheet("By Day")
    day_ws.append(["Date", "Events"])
    _style_header(day_ws, 2)
    for day in sorted(per_day):
        day_ws.append([day, per_day[day]])
    n_days = len(per_day)
    if n_days:
        data = Reference(day_ws, min_col=2, min_row=1, max_row=n_days + 1)
        cats = Reference(day_ws, min_col=1, min_row=2, max_row=n_days + 1)
        chart = LineChart()
        chart.title = "Events per day"
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.height = 9
        chart.width = 18
        day_ws.add_chart(chart, "D2")
    _autosize(day_ws)

    wb.save(out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        default=os.path.join(HERE, "activity.log"),
        help="Path to the activity log file (default: ./activity.log)",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(HERE, "activity_report.xlsx"),
        help="Output .xlsx path (default: ./activity_report.xlsx)",
    )
    parser.add_argument(
        "--no-geo",
        action="store_true",
        help="Skip online IP->country lookups (offline mode)",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.log):
        parser.error(f"log file not found: {args.log}")

    print(f"Reading log: {args.log}")
    records = parse_log(args.log)
    print(f"Parsed {len(records)} activity records")

    unique_ips = sorted({r["ip"] for r in records if r["ip"]})
    print(f"Resolving {len(unique_ips)} unique IP(s) to countries...")
    ip_country = resolve_countries(unique_ips, use_geo=not args.no_geo)
    for ip in unique_ips:
        print(f"  {ip:40s} -> {ip_country[ip]}")

    build_report(records, ip_country, args.out)
    print(f"Wrote report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
