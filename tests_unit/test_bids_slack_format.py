import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app.bids.slack_format import Owner, format_report
from app.models.bid_report import BidReportRow

CT = ZoneInfo("America/Chicago")
START = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)
PEDRO, MARIA = PydanticObjectId(), PydanticObjectId()
OWNERS = {str(PEDRO): Owner("Pedro", "<@U1>"), str(MARIA): Owner("maria", "Maria")}


def row(owner, name, count, rate=0.5, country=None, profile=None, error=None):
    return BidReportRow(kind="daily", periodKey="2026-10-02", periodStart=START, periodEnd=END,
                        bidderId=PydanticObjectId(), ownerId=owner, bidderName=name, country=country,
                        profileName=profile, rate=rate, count=count,
                        amount=None if count is None else round(count * rate, 2), error=error)


def texts(messages):
    return [b["text"]["text"] for m in messages for b in m if b["type"] == "section"]


def test_daily_report_copy():
    rows = [row(PEDRO, "Raj Kumar", 55, country="IN", profile="Backend"),
            row(PEDRO, "Ana Silva", 87, country="BR", profile="AI Full-stack"),
            row(PEDRO, "John Doe", None, error="AccessDenied"),
            row(MARIA, "Mia Cruz", 0)]
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, "https://app.example")
    assert text == "Daily bids — Fri Oct 2: 142 bids"
    t = texts(messages)
    assert t[0] == "📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT"
    assert t[1] == "*Maria* — 0 bids\n• Mia Cruz — 0"   # "maria" sorts before "Pedro"
    assert t[2] == ("*<@U1>* — 142 bids · 1 failed\n• 🇧🇷 Ana Silva · AI Full-stack — 87\n"
                    "• 🇮🇳 Raj Kumar · Backend — 55\n• ⚠️ John Doe — couldn't read screenshot folder")
    assert t[-1] == "*Total: 142 bids · 4 bidders* · <https://app.example/bidders|View in app>"


def test_weekly_report_lines_include_pay():
    rows = [row(PEDRO, "Ana Silva", 512, rate=0.5, country="BR", profile="AI Full-stack"),
            row(PEDRO, "Raj Kumar", 1, rate=0.1)]
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, "https://app.example")
    t = texts(messages)
    assert t[0].startswith("💰 *Weekly bids & pay — week ending Fri Oct 2* · Thu Oct 1 6:30 PM → Fri Oct 2 6:30 PM CDT")
    assert t[1] == ("*<@U1>* — 513 bids · *$256.10*\n• 🇧🇷 Ana Silva · AI Full-stack — 512 bids × $0.50 = $256.00\n"
                    "• Raj Kumar — 1 bid × $0.10 = $0.10")
    assert t[-1] == "*Total: 513 bids · $256.10* · <https://app.example/bidders|View in app>"
    assert text == "Weekly bids & pay — week ending Fri Oct 2: 513 bids · $256.10"


def test_missing_owner_falls_back():
    stranger = PydanticObjectId()
    _, messages = format_report("daily", START, END, [row(stranger, "Lee", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[1] == "*Unknown owner* — 3 bids\n• Lee — 3"


def test_large_reports_respect_slack_limits():
    owners = {str(o): Owner(f"Owner {i}", f"Owner {i}") for i, o in enumerate(PydanticObjectId() for _ in range(60))}
    # 5 bidders × ~620 chars per owner forces the 3,000-char section split; 60 owners force a 2nd message
    rows = [row(PydanticObjectId(o), f"Bidder {i:03d} " + "x" * 600, i)
            for i, o in enumerate(list(owners) * 5)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://a")
    assert len(messages) > 1 and all(len(m) <= 50 for m in messages)
    assert all(len(s) <= 3000 for s in texts(messages))
    body = "\n".join(texts(messages))
    assert all(body.count(f"Bidder {i:03d} ") == 1 for i in range(300))


def test_user_text_is_escaped_but_slack_mentions_are_kept():
    hostile = "Ana <!channel> & co"
    owners = {**OWNERS, str(MARIA): Owner("maria", "<https://evil.example|Maria>")}
    rows = [row(PEDRO, hostile, 2, profile="<https://evil.example|click>"),
            row(PEDRO, "Bad <!here>", None, error="AccessDenied"),
            row(MARIA, "Mia", 1)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://app.example")
    t = texts(messages)
    assert t[1] == "*&lt;https://evil.example|Maria&gt;* — 1 bid\n• Mia — 1"
    assert t[2] == ("*<@U1>* — 2 bids · 1 failed\n"
                    "• Ana &lt;!channel&gt; &amp; co · &lt;https://evil.example|click&gt; — 2\n"
                    "• ⚠️ Bad &lt;!here&gt; — couldn't read screenshot folder")
    assert "<!" not in "".join(t)


def test_daily_footer_says_bidder_for_one():
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 3 bids · 1 bidder*")


def counted(kind, pending, rejected, count=2):
    r = row(PEDRO, "Ana Silva", count, country="BR")
    return r.model_copy(update={"kind": kind, "pending": pending, "rejected": rejected})


def test_report_line_shows_pending_and_rejected():
    weekly_text, weekly = format_report("weekly", START, END, [counted("weekly", 1, 1)], OWNERS, CT, "https://app.example")
    t = texts(weekly)
    assert t[1] == ("*<@U1>* — Approved 2 bids (pending 1, rejected 1) · *$1.00*\n"
                    "• 🇧🇷 Ana Silva — Approved 2 bids × $0.50 = $1.00 (pending 1, rejected 1)")
    assert t[2] == "*Total: Approved 2 bids (pending 1, rejected 1) · $1.00* · <https://app.example/bidders|View in app>"
    assert weekly_text == "Weekly bids & pay — week ending Fri Oct 2: Approved 2 bids (pending 1, rejected 1) · $1.00"

    daily_text, daily = format_report("daily", START, END, [counted("daily", 3, 0)], OWNERS, CT, "https://app.example")
    t = texts(daily)
    assert t[1] == ("*<@U1>* — Approved 2 bids (pending 3, rejected 0)\n"
                    "• 🇧🇷 Ana Silva — Approved 2 (pending 3, rejected 0)")
    assert t[2] == "*Total: Approved 2 bids (pending 3, rejected 0) · 1 bidder* · <https://app.example/bidders|View in app>"
    assert daily_text == "Daily bids — Fri Oct 2: Approved 2 bids (pending 3, rejected 0)"


def test_report_totals_add_up_pending_and_rejected_across_bidders():
    rows = [counted("daily", 1, 2, count=3), counted("daily", 4, 0, count=1)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, "https://app.example")
    assert texts(messages)[1].splitlines()[0] == "*<@U1>* — Approved 4 bids (pending 5, rejected 2)"


def test_rows_without_pending_keep_the_folder_line():
    _, weekly = format_report("weekly", START, END, [counted("weekly", None, None)], OWNERS, CT, "https://app.example")
    assert texts(weekly)[1] == "*<@U1>* — 2 bids · *$1.00*\n• 🇧🇷 Ana Silva — 2 bids × $0.50 = $1.00"
    assert texts(weekly)[2].startswith("*Total: 2 bids · $1.00*")
