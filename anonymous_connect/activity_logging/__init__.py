"""Dedicated user-activity logging package.

This package is intentionally separate from the rest of the app so all
activity-tracking concerns live in one place. Import ``log_event`` (or one of
the thin ``log_*`` wrappers) and call it from views / consumers.

    from activity_logging import log_event, Action

    log_event(Action.VISIT, request=request)

Logs are written as single-line, key=value records to a dedicated file
(see ``LOGGING`` in settings) so they are easy to grep now and parse into a
report later.
"""

from .logger import Action, log_event

__all__ = ["Action", "log_event"]
