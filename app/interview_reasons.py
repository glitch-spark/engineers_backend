"""Why a round was canceled / rescheduled or rejected: fixed keys (the contract with the app) and their labels.
Used by the interviews router and the Slack forms. docs/superpowers/specs/2026-11-20-interview-reasons-design.md"""

from typing import Optional

# Canceled and rescheduled share one list.
CANCEL_REASONS: dict[str, str] = {
    "company": "Company",
    "me": "Me",
    "caller": "Caller",
    "job_mismatch": "Job doesn't match",
    "technical": "Technical issue",
}
CANCEL_REASON_HELP: dict[str, str] = {
    "company": "They canceled or moved it, or the interviewer didn't join.",
    "me": "I didn't confirm the time clearly with the company or the caller.",
    "caller": "The caller had a schedule conflict or a personal matter.",
    "job_mismatch": "The location, no remote work or another requirement ruled it out.",
    "technical": "Audio, video, internet or the meeting link failed.",
}
REJECT_REASONS: dict[str, str] = {
    "no_reason": "No reason given",
    "no_answer": "No answer from the company",
    "location": "Location, work permit or clearance",
    "language": "Language or communication",
    "technical_answers": "Weak technical answers",
    "experience": "Experience didn't match (stack, seniority, domain)",
    "background": "Slip in the background story",
    "test": "Failed test or take-home",
    "pay": "Pay or contract terms",
    "other_candidate": "Another candidate chosen or position filled",
    "av": "Audio or video problem",
    "other": "Other",
}

_BY_STATUS = {"canceled": CANCEL_REASONS, "rejected": REJECT_REASONS}


def valid_status_reason(status: Optional[str], reason: str) -> bool:
    """A reason key from the list of this round status; any other status takes no reason."""
    return reason in _BY_STATUS.get(status or "", {})
