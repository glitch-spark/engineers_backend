"""Slack forms (modals) for the coordination channel. Tasks 8 and 9 fill in the views and the submission handling.

`modals` never imports `actions` at module level: `actions` imports this module, and the submission handler needs the
`actions` helpers, so it imports them inside the function."""

from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

CANCEL_VIEW = "coord_cancel"
RESCHEDULE_VIEW = "coord_reschedule"
REJECT_VIEW = "coord_reject"
COORD_VIEWS = frozenset({CANCEL_VIEW, RESCHEDULE_VIEW, REJECT_VIEW})


def submission_errors(view: dict, now: datetime) -> dict[str, str]:
    """Field errors to show inline in the form; empty when the submission is fine."""
    return {}


async def process_coordination_submission(view: dict, slack_user_id: str) -> None:
    """Apply a submitted form. Never raises."""
    log.info("coordination submission for %s: %s", slack_user_id, view.get("callback_id"))
