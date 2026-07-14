"""Seed the EmailRule collection with ATS template fingerprints.

Run once after deploying the email-sync feature:
  python -m scripts.seed_email_rules

Idempotent — uses upsert on (senderPattern, phrasePattern). Re-running
just refreshes labels/confidence on existing rows.

Rules are intentionally narrow — sender domain + specific phrase. The
phrase is the *canonical confirmation language* the ATS template uses,
not generic vocabulary. That keeps precision high (1.0 confidence is
the default for seed rows because a Greenhouse "Thanks for applying"
template is essentially never wrong).
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow running this as `python -m scripts.seed_email_rules` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beanie import init_beanie  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models import EmailRule  # noqa: E402


# (senderPattern, phrasePattern, label, stageRound, notes)
SEED_RULES: list[tuple[str, str, str, str | None, str]] = [
    # --- applied confirmations ---
    ("@greenhouse-mail.com", "thanks for applying to", "applied", "bid_sent", "Greenhouse application receipt"),
    ("@greenhouse-mail.com", "we've received your application", "applied", "bid_sent", "Greenhouse alt"),
    ("@greenhouse-mail.com", "your application has been received", "applied", "bid_sent", "Greenhouse alt2"),
    (".lever.co", "thanks for applying", "applied", "bid_sent", "Lever receipt"),
    (".lever.co", "we've received your application", "applied", "bid_sent", "Lever alt"),
    ("@ashbyhq.com", "application received", "applied", "bid_sent", "Ashby receipt"),
    ("@ashbyhq.com", "thank you for applying", "applied", "bid_sent", "Ashby alt"),
    ("@myworkday.com", "your application has been submitted", "applied", "bid_sent", "Workday receipt"),
    ("@myworkday.com", "thank you for your application", "applied", "bid_sent", "Workday alt"),
    ("@smartrecruiters.com", "thank you for applying", "applied", "bid_sent", "SmartRecruiters"),
    ("@workable.com", "thank you for applying", "applied", "bid_sent", "Workable receipt"),
    ("@workable.com", "we have received your application", "applied", "bid_sent", "Workable alt"),
    ("@jobvite.com", "thank you for your interest", "applied", "bid_sent", "Jobvite receipt"),
    ("@taleo.net", "your application has been received", "applied", "bid_sent", "Taleo receipt"),
    ("@icims.com", "thank you for applying", "applied", "bid_sent", "iCIMS receipt"),
    ("@bamboohr.com", "thank you for applying", "applied", "bid_sent", "BambooHR receipt"),
    ("@breezy.hr", "thanks for applying", "applied", "bid_sent", "Breezy receipt"),
    ("@recruiterbox.com", "thank you for applying", "applied", "bid_sent", "Recruiterbox receipt"),
    ("@notion.com", "thanks for applying to notion", "applied", "bid_sent", "Notion direct"),
    ("@stripe.com", "thanks for applying to stripe", "applied", "bid_sent", "Stripe direct"),

    # --- generic, sender-agnostic application receipts (empty sender = ANY
    # sender). These fire before the LLM, so every phrase must be
    # confirmation-only — verified to NOT appear in rejection emails.
    # "thank you for applying" is deliberately EXCLUDED: it shows up in
    # rejection preambles, so the LLM handles that phrasing instead.
    ("", "thanks for applying", "applied", "bid_sent", "generic ATS receipt"),
    ("", "we received your application", "applied", "bid_sent", "generic receipt"),
    ("", "we've received your application", "applied", "bid_sent", "generic receipt"),
    ("", "we have received your application", "applied", "bid_sent", "generic receipt"),
    ("", "your application has been received", "applied", "bid_sent", "generic receipt"),
    ("", "your resume has been received", "applied", "bid_sent", "generic receipt"),
    ("", "acknowledgement to application", "applied", "bid_sent", "generic receipt"),
    ("", "acknowledgement of your application", "applied", "bid_sent", "generic receipt"),
    ("", "successfully submitted your application", "applied", "bid_sent", "generic receipt"),
    ("", "application has been submitted", "applied", "bid_sent", "generic receipt"),

    # --- rejections (high-precision phrases) ---
    ("@greenhouse-mail.com", "decided to move forward with other candidates", "rejection", "rejected", "Greenhouse rejection"),
    (".lever.co", "after careful consideration", "rejection", "rejected", "Lever rejection"),
    (".lever.co", "not moving forward", "rejection", "rejected", "Lever rejection alt"),
    ("@ashbyhq.com", "we have decided not to move forward", "rejection", "rejected", "Ashby rejection"),
    ("@workable.com", "we will not be moving forward", "rejection", "rejected", "Workable rejection"),
    ("@myworkday.com", "we have decided to pursue other candidates", "rejection", "rejected", "Workday rejection"),
    ("@smartrecruiters.com", "not be moving forward", "rejection", "rejected", "SR rejection"),

    # --- assessment platforms ---
    ("@hackerrank.com", "invited you to take", "take_home", "tech", "HackerRank assignment"),
    ("@hackerrank.com", "complete the test", "take_home", "tech", "HackerRank reminder"),
    ("@codility.com", "coding test", "take_home", "tech", "Codility"),
    ("@codility.com", "complete the assessment", "take_home", "tech", "Codility alt"),
    ("@coderpad.io", "interview scheduled", "live_coding", "live_coding", "CoderPad"),
    ("@karat.com", "karat interview", "live_coding", "live_coding", "Karat"),
    ("@triplebyte.com", "assessment", "take_home", "tech", "Triplebyte"),

    # --- one-way / recorded video interviews → ai_interview column ---
    ("@hireflix.com", "video interview", "take_home", "ai_interview", "Hireflix one-way"),
    ("@sparkhire.com", "video interview", "take_home", "ai_interview", "SparkHire one-way"),
    ("@hirevue.com", "on-demand interview", "take_home", "ai_interview", "HireVue"),
    ("@willo.video", "video interview", "take_home", "ai_interview", "Willo one-way"),
    ("", "one-way video interview", "take_home", "ai_interview", "generic one-way video"),
    ("", "record your responses", "take_home", "ai_interview", "recorded video"),
    ("", "on-demand interview", "take_home", "ai_interview", "on-demand video"),
    ("", "asynchronous video interview", "take_home", "ai_interview", "async video"),

    # --- scheduling ---
    ("@calendly.com", "scheduled a meeting", "schedule_interview", "intro", "Calendly confirmation"),
    ("@calendly.com", "new event scheduled", "schedule_interview", "intro", "Calendly alt"),
    ("@goodtime.io", "interview scheduled", "schedule_interview", "intro", "GoodTime"),
    ("@x.ai", "interview confirmed", "schedule_interview", "intro", "x.ai confirm"),

    # --- offers (rare via ATS; usually direct mail; keep narrow) ---
    (".lever.co", "offer letter", "offer", "offer", "Lever offer (rare)"),

    # --- noise (we also have a code-level blocklist; these are belt-and-braces) ---
    ("@alerts.jobot.com", "jobs like", "noise", None, "Jobot alerts"),
    ("@jobot.com", "apply now", "noise", None, "Jobot generic"),
    ("@alerts.wellfound.com", "jobs", "noise", None, "Wellfound alerts"),
    ("jobs-noreply@linkedin.com", "jobs", "noise", None, "LinkedIn job alerts"),
    ("@indeed.com", "jobs", "noise", None, "Indeed alerts"),
    ("@ziprecruiter.com", "jobs", "noise", None, "ZipRecruiter alerts"),
]


async def run() -> int:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    database = client.get_default_database()
    await init_beanie(database=database, document_models=[EmailRule])

    inserted = 0
    updated = 0
    for sender, phrase, label, stage, notes in SEED_RULES:
        existing = await EmailRule.find_one(
            EmailRule.senderPattern == sender,
            EmailRule.phrasePattern == phrase,
        )
        if existing is None:
            await EmailRule(
                senderPattern=sender,
                phrasePattern=phrase,
                label=label,
                stageRound=stage,
                confidence=1.0,
                source="seed",
                notes=notes,
            ).insert()
            inserted += 1
        else:
            existing.label = label
            existing.stageRound = stage
            existing.notes = notes or existing.notes
            existing.source = "seed"
            await existing.save()
            updated += 1

    print(f"inserted {inserted}, updated {updated}, total {inserted + updated}")
    return 0


if __name__ == "__main__":
    asyncio.run(run())
