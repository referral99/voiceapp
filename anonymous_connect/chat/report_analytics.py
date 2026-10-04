"""Build the data for the password-protected /report.html dashboard.

Everything is derived from the activity log written by
``activity_logging.logger.log_event`` (one ``ACTIVITY action=... key=value``
line per user action, timestamps in IST). This module only *reads* that file
and never mutates app state, so it is safe to call on every report request.

The public entry point is :func:`build_report_data`, which returns a plain
dict ready to drop into the ``report.html`` template context.

Key metrics produced
--------------------
* Per-hour traffic for the current month (and a 0-23 "hour of day" histogram
  for the traffic graph).
* Totals for the month: site visits, call hits (voice), message hits,
  premium-button clicks, logged-in users, paying users + revenue.
* Visitors by gender (optional, flag-gated).
* Visitors by country (optional geo lookup, cached on disk).

The heavy log parsing / IP resolution helpers are reused from the sibling
``activity_report.py`` script so there is a single source of truth.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime

from django.conf import settings

# Reuse the battle-tested log parser + country resolver from the CLI report
# script that already lives next to the project. Importing by file path keeps
# this module working regardless of how the script is packaged.
import importlib.util

_SCRIPT_PATH = os.path.join(settings.BASE_DIR, "activity_report.py")


def _load_report_script():
    spec = importlib.util.spec_from_file_location("activity_report", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Action names mirrored from activity_logging.logger.Action. Kept as literals
# here so the report never breaks if the logger module is refactored.
A_VISIT = "visit"
A_MSG = "msg_call_attempt"
A_VOICE = "voice_call_attempt"
A_PREMIUM_CLICK = "premium_click"
A_PREMIUM_PAID = "premium_paid"
A_GENDER = "gender_selected"


def _default_log_path():
    """Where the live activity log lives (settings.ACTIVITY_LOG_DIR)."""
    log_dir = getattr(settings, "ACTIVITY_LOG_DIR", settings.BASE_DIR)
    return os.path.join(str(log_dir), "activity.log")


def _is_logged_in(user):
    return isinstance(user, str) and user.startswith("user:")


def _month_key(dt):
    return dt.strftime("%Y-%m")


def build_report_data(log_path=None, use_geo=None, include_gender=None, month=None):
    """Parse the activity log and return a context dict for the template.

    Args:
        log_path: override path to activity.log (defaults to the live log).
        use_geo: resolve IPs to countries. Defaults to settings.REPORT_GEO_LOOKUP.
        include_gender: compute the gender breakdown. Defaults to
            settings.REPORT_GENDER_BREAKDOWN.
        month: "YYYY-MM" to report on. Defaults to the most recent month that
            has data (falls back to the current month).
    """
    if use_geo is None:
        use_geo = getattr(settings, "REPORT_GEO_LOOKUP", True)
    if include_gender is None:
        include_gender = getattr(settings, "REPORT_GENDER_BREAKDOWN", True)

    log_path = log_path or _default_log_path()

    script = _load_report_script()

    records = []
    if os.path.exists(log_path):
        records = script.parse_log(log_path)

    # ---- Choose the reporting month ------------------------------------- #
    dated = [r for r in records if r["datetime"]]
    months_present = sorted({_month_key(r["datetime"]) for r in dated})
    if month is None:
        month = months_present[-1] if months_present else datetime.now().strftime("%Y-%m")

    month_records = [r for r in dated if _month_key(r["datetime"]) == month]

    # ---- Per-hour-of-day traffic (visits) for the graph ----------------- #
    # "Traffic" = site visits. Indexed 0..23 so the chart always has a full day.
    hour_of_day = [0] * 24
    for r in month_records:
        if r["action"] == A_VISIT:
            hour_of_day[r["datetime"].hour] += 1

    # ---- Per-hour timeline across the month (chronological buckets) ----- #
    per_hour = defaultdict(int)  # "YYYY-MM-DD HH:00" -> visits
    for r in month_records:
        if r["action"] == A_VISIT:
            bucket = r["datetime"].strftime("%Y-%m-%d %H:00")
            per_hour[bucket] += 1
    per_hour_rows = [{"hour": k, "visits": per_hour[k]} for k in sorted(per_hour)]

    # ---- Headline totals ------------------------------------------------ #
    visits = sum(1 for r in month_records if r["action"] == A_VISIT)
    msg_hits = sum(1 for r in month_records if r["action"] == A_MSG)
    voice_hits = sum(1 for r in month_records if r["action"] == A_VOICE)
    premium_clicks = sum(1 for r in month_records if r["action"] == A_PREMIUM_CLICK)

    unique_visitors = len({r["user"] for r in month_records if r["user"] and r["action"] == A_VISIT})
    logged_in_users = sorted(
        {r["user"] for r in month_records if _is_logged_in(r["user"])}
    )

    # ---- Paying users + revenue ----------------------------------------- #
    paying_users = set()
    revenue = 0.0
    payments = []
    for r in month_records:
        if r["action"] != A_PREMIUM_PAID:
            continue
        paying_users.add(r["user"])
        amount_raw = r["extra"].get("amount")
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            amount = 0.0
        revenue += amount
        payments.append(
            {
                "user": r["user"],
                "amount": amount,
                "order_id": r["extra"].get("order_id", "-"),
                "payment_id": r["extra"].get("payment_id", "-"),
                "when": r["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    # ---- Visitors by gender (flag-gated) -------------------------------- #
    gender_counts = None
    if include_gender:
        gc = Counter()
        for r in month_records:
            if r["action"] == A_GENDER:
                gender = (r["extra"].get("gender") or "unknown").strip() or "unknown"
                gc[gender] += 1
        gender_counts = dict(gc.most_common())

    # ---- Visitors by country (optional geo) ----------------------------- #
    unique_ips = sorted({r["ip"] for r in month_records if r["ip"]})
    ip_country = script.resolve_countries(unique_ips, use_geo=use_geo)
    country_counter = Counter()
    for r in month_records:
        if r["action"] == A_VISIT:
            country_counter[ip_country.get(r["ip"], "Unknown")] += 1
    country_counts = dict(country_counter.most_common())

    # ---- Time span ------------------------------------------------------ #
    span = ""
    if month_records:
        dts = [r["datetime"] for r in month_records]
        span = f"{min(dts):%Y-%m-%d %H:%M}  ->  {max(dts):%Y-%m-%d %H:%M}"

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "month": month,
        "months_present": months_present,
        "time_span": span,
        "totals": {
            "visits": visits,
            "unique_visitors": unique_visitors,
            "msg_hits": msg_hits,
            "voice_hits": voice_hits,
            "premium_clicks": premium_clicks,
            "logged_in_users": len(logged_in_users),
            "paying_users": len(paying_users),
            "revenue": round(revenue, 2),
        },
        "logged_in_user_ids": logged_in_users,
        "payments": payments,
        "hour_of_day": hour_of_day,              # list[24] for the bar chart
        "per_hour_rows": per_hour_rows,          # chronological per-hour table
        "gender_counts": gender_counts,          # dict or None if flag off
        "country_counts": country_counts,        # dict
        "gender_enabled": bool(include_gender),
        "geo_enabled": bool(use_geo),
    }
