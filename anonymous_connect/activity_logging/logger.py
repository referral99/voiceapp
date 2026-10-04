"""Core activity-logging helpers.

Everything is funnelled through :func:`log_event`, which emits one line per
user action to the ``activity`` logger. The line format is stable and
machine-friendly so we can build reports from it later::

    ACTIVITY action=visit ts=2026-10-03T18:04:56+05:30 user=guest:abcd1234 ip=1.2.3.4 ...

Design goals:
* Separate from the rest of the app (lives in its own package).
* Never raise. Logging must not break a real user request, so every public
  entry point swallows its own errors.
* No hard dependency on a request object - callers may pass a ``request``, a
  ``UserProfile``, or neither.
"""

import logging
from zoneinfo import ZoneInfo

from django.utils import timezone

# Render activity timestamps in Indian Standard Time (IST, UTC+05:30) so the
# logs match the operator's local wall clock regardless of settings.TIME_ZONE.
IST = ZoneInfo("Asia/Kolkata")

# Dedicated logger. Its handlers/level are configured in settings.LOGGING under
# the matching name, so it writes to its own file independent of app logs.
activity_logger = logging.getLogger("activity")


class Action:
    """Canonical action names. Using constants keeps log values consistent."""

    VISIT = "visit"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    MSG_CALL_ATTEMPT = "msg_call_attempt"
    VOICE_CALL_ATTEMPT = "voice_call_attempt"
    AGE_SELECTED = "age_selected"
    GENDER_SELECTED = "gender_selected"
    PROFESSION_SELECTED = "profession_selected"
    PREMIUM_CLICK = "premium_click"
    PREMIUM_PAID = "premium_paid"
    PREMIUM_PAYMENT_FAILED = "premium_payment_failed"


def _client_ip(request):
    """Best-effort client IP, honouring the first proxy hop if present."""
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _user_identifier(request=None, profile=None):
    """A stable, non-PII-ish identifier for whoever performed the action.

    Prefers the authenticated user id, falling back to the guest session key.
    Returns a string like ``user:42`` or ``guest:abcd1234`` or ``anonymous``.
    """
    try:
        if profile is not None:
            if getattr(profile, "user_id", None):
                return f"user:{profile.user_id}"
            if getattr(profile, "session_id", None):
                return f"guest:{profile.session_id}"

        if request is not None:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                return f"user:{user.id}"
            session = getattr(request, "session", None)
            key = getattr(session, "session_key", None) if session else None
            if key:
                return f"guest:{key}"
    except Exception:  # noqa: BLE001 - identity is best-effort only
        pass
    return "anonymous"


def _format_value(value):
    """Render a single field value for the key=value line."""
    if value is None:
        return "-"
    text = str(value)
    # Keep each record on one line and keep key=value parsing simple.
    text = text.replace("\n", " ").replace("\r", " ")
    if " " in text:
        return f'"{text}"'
    return text


def log_event(action, request=None, profile=None, **fields):
    """Emit one activity record.

    Args:
        action: one of the :class:`Action` constants (or any string).
        request: optional Django ``HttpRequest`` - used to derive IP + identity.
        profile: optional ``UserProfile`` - used to derive identity.
        **fields: any extra key=value details (e.g. ``mode='voice'``,
            ``age=27``, ``amount=10``). ``None`` values are rendered as ``-``.

    Never raises.
    """
    try:
        parts = [
            f"action={_format_value(action)}",
            f"ts={_format_value(timezone.now().astimezone(IST).isoformat())}",
            f"user={_format_value(_user_identifier(request, profile))}",
            f"ip={_format_value(_client_ip(request))}",
        ]
        for key, value in fields.items():
            parts.append(f"{key}={_format_value(value)}")

        activity_logger.info("ACTIVITY %s", " ".join(parts))
    except Exception:  # noqa: BLE001 - logging must never break the request
        # Fall back to the standard logger so we at least notice breakages.
        logging.getLogger(__name__).exception("Failed to write activity log")
