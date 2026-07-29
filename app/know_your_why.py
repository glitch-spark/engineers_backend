"""Daily “Know your why” quotes for Slack digest DMs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Union

KNOW_YOUR_WHY_QUOTES: tuple[str, ...] = (
    "Strangers won't remember your effort. The people who matter will.",
    "You're not behind. You're building something that takes time.",
    "One honest conversation beats ten perfect resumes.",
    "The interview is practice for the life you want — show up curious.",
    "Rejection is data, not a verdict on your worth.",
    "Your why is quieter than the noise. Listen for it anyway.",
    "Ship the draft. Clarity comes after courage.",
    "Today's awkward question is tomorrow's fluent answer.",
    "You don't need every door. You need the right one open.",
    "Consistency is a kindness you give your future self.",
    "Talent opens rooms. Character decides if you stay.",
    "Rest is part of the work — empty batteries don't negotiate.",
    "Compare less. Compound more.",
    "The story you tell yourself becomes the story they hear.",
    "Small progress on hard days still counts as progress.",
    "Ask for the thing. Waiting is also a decision.",
    "You are allowed to want more without apologizing for it.",
    "Preparation is respect — for them and for you.",
    "Keep the receipt of every time you showed up scared and did it anyway.",
    "Your network is people you've helped, not people you've impressed.",
    "Focus on the next useful step, not the whole staircase.",
    "Doubt visits everyone. Don't give it the keys.",
    "The calendar doesn't care about perfect. It cares about present.",
    "Build a reputation for finishing what you start.",
    "Remember why you started before the metrics get loud.",
)


def know_your_why_for_date(day: Union[date, datetime, str]) -> str:
    """Stable quote for a calendar day (YYYY-MM-DD or date/datetime)."""
    if isinstance(day, datetime):
        key = day.date().isoformat()
    elif isinstance(day, date):
        key = day.isoformat()
    else:
        key = str(day).strip()[:10]
    if len(key) < 10:
        key = date.today().isoformat()
    n = sum(ord(c) for c in key)
    return KNOW_YOUR_WHY_QUOTES[n % len(KNOW_YOUR_WHY_QUOTES)]
