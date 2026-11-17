import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app.bids.slack_format import Owner, _short_profile, format_report
from app.models.bid_report import BidReportRow

CT = ZoneInfo("America/Chicago")
START = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)
PEDRO, MARIA = PydanticObjectId(), PydanticObjectId()
OWNERS = {str(PEDRO): Owner("Pedro", "<@U1>"), str(MARIA): Owner("maria", "Maria")}
APP = "https://app.example"


def row(owner, name, count, rate=0.5, country=None, profile=None, error=None):
    return BidReportRow(kind="daily", periodKey="2026-10-02", periodStart=START, periodEnd=END,
                        bidderId=PydanticObjectId(), ownerId=owner, bidderName=name, country=country,
                        profileName=profile, rate=rate, count=count,
                        amount=None if count is None else round(count * rate, 2), error=error)


def rec(owner, name, count, pending, rejected=0, rate=0.5, country=None, profile=None):
    """A bid-record row: count is the approved bids."""
    return row(owner, name, count, rate=rate, country=country, profile=profile).model_copy(
        update={"pending": pending, "rejected": rejected})


def texts(messages):
    return [b["text"]["text"] for m in messages for b in m if b["type"] == "section"]


def accessories(messages):
    return [b["accessory"] for m in messages for b in m if "accessory" in b]


def test_daily_post_lists_only_bidders_with_bids():
    rows = [rec(PEDRO, "Ana Silva", 160, 12, country="BR", profile="AI Full-stack"),
            rec(PEDRO, "Raj Kumar", 0, 0),
            row(PEDRO, "John Doe", None, error="AccessDenied"),
            rec(MARIA, "Mia Cruz", 0, 0)]
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    assert text == "Daily bids — Fri Oct 2: 160 approved · 12 pending"
    assert texts(messages) == [
        "📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT",
        "*<@U1>* — 160 approved · 12 pending · 1 failed",
        "• 🇧🇷 Ana Silva · AI Full-stack — 160 approved · 12 pending\n"
        "• ⚠️ John Doe — couldn't read screenshot folder",
        "*Total: 160 approved · 12 pending · 1 bidder with bids* · <https://app.example/bidders|View in app>",
    ]


def test_owner_button_links_to_the_review():
    rows = [rec(PEDRO, "Ana Silva", 160, 12), rec(MARIA, "Mia Cruz", 1, 0)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    assert accessories(messages) == [  # "maria" sorts before "Pedro"
        {"type": "button", "text": {"type": "plain_text", "text": "Open Bids"},
         "url": f"https://app.example/bidders?owner={MARIA}&range=pay_week&date=2026-10-02",
         "action_id": f"bids-link-{MARIA}"},
        {"type": "button", "text": {"type": "plain_text", "text": "Review 12 pending"},
         "url": f"https://app.example/bidders?owner={PEDRO}&mode=focus&review=day&at=2026-10-02",
         "action_id": f"bids-link-{PEDRO}"},
    ]


def test_period_key_falls_back_to_the_end_day_without_rows():
    _, messages = format_report("daily", START, END, [], OWNERS, CT, APP, my_bids={str(PEDRO): {"Cory": 3}})
    assert accessories(messages)[0]["url"] == f"https://app.example/bidders?owner={PEDRO}&range=pay_week&date=2026-10-02"


def test_rejected_only_when_some():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 5, 0, 2)], OWNERS, CT, APP)
    t = texts(messages)
    assert t[1] == "*<@U1>* — 5 approved · 0 pending · 2 rejected"
    assert t[2] == "• Ana — 5 approved · 0 pending · 2 rejected"
    assert t[3].startswith("*Total: 5 approved · 0 pending · 2 rejected · 1 bidder with bids*")


def test_weekly_post_pay_lines_and_open_pay():
    rows = [rec(PEDRO, "Ana Silva", 160, 12, rate=0.06, country="BR", profile="AI Full-stack"),
            rec(MARIA, "Mia Cruz", 0, 0, rate=0.06)]
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, APP)
    assert text == "Weekly bids & pay — week ending Fri Oct 2: 160 approved · 12 pending · $9.60"
    t = texts(messages)
    assert len(t) == 4
    assert t[1] == "*<@U1>* — 160 approved · 12 pending · *$9.60*"
    assert t[2] == "• 🇧🇷 Ana Silva · AI Full-stack — 160 approved × $0.06 = $9.60 · 12 pending"
    assert t[3] == "*Total: 160 approved · 12 pending · $9.60 · 1 bidder with bids* · <https://app.example/bidders|View in app>"
    assert accessories(messages) == [
        {"type": "button", "text": {"type": "plain_text", "text": "Open Pay"},
         "url": f"https://app.example/bidders?section=pay&owner={PEDRO}&range=pay_week&date=2026-10-02",
         "action_id": f"bids-link-{PEDRO}"}]
    assert "accessory" not in messages[0][-1] and "accessory" not in messages[0][2]


def test_no_bids_says_so():
    rows = [rec(PEDRO, "Ana", 0, 0), row(MARIA, "Mia", 0)]
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    assert texts(messages) == ["📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT", "No bids today.",
                               "<https://app.example/bidders|View in app>"]
    assert text.endswith(": no bids") and accessories(messages) == []
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, APP)
    assert texts(messages)[1] == "No bids this week." and text.endswith(": no bids")


def test_my_bids_line_per_owner():
    rows = [rec(PEDRO, "Ana Silva", 160, 12), rec(MARIA, "Mia Cruz", 0, 0)]
    my = {str(PEDRO): {"Cory": 23, "Arda": 12, "Zed": 0}, str(MARIA): {"Solo": 5}}
    _, plain = format_report("daily", START, END, rows, OWNERS, CT, APP)
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP, my_bids=my)
    t = texts(messages)
    assert t[1:3] == ["*Maria*", "_My bids · 5 — Solo 5_"]
    assert t[3] == "*<@U1>* — 160 approved · 12 pending"
    assert t[4].endswith("\n_My bids · 35 — Arda 12 · Cory 23_")
    assert t[-1] == texts(plain)[-1] == "*Total: 160 approved · 12 pending · 1 bidder with bids* · <https://app.example/bidders|View in app>"
    assert [a["text"]["text"] for a in accessories(messages)] == ["Open Bids", "Review 12 pending"]


def test_my_bids_names_are_escaped_sorted_and_unknown_owners_survive():
    stranger = str(PydanticObjectId())
    my = {str(PEDRO): {"zed": 1, "Bob <!channel>": 2, "alice": 3}, stranger: {"Solo": 1}, str(MARIA): {"Zero": 0}}
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP, my_bids=my)
    t = texts(messages)
    assert t[t.index("*Unknown owner*") + 1] == "_My bids · 1 — Solo 1_"
    assert t[2].endswith("\n_My bids · 6 — alice 3 · Bob &lt;!channel&gt; 2 · zed 1_")
    assert not any("Maria" in x for x in t)  # zero My bids and no rows: no section


def test_folder_rows_keep_the_bids_wording():
    folder = row(PEDRO, "Ana Silva", 2, country="BR")
    _, messages = format_report("weekly", START, END, [folder], OWNERS, CT, APP)
    t = texts(messages)
    assert t[1:3] == ["*<@U1>* — 2 bids · *$1.00*", "• 🇧🇷 Ana Silva — 2 bids × $0.50 = $1.00"]
    assert t[3].startswith("*Total: 2 bids · $1.00 · 1 bidder with bids*")
    _, daily = format_report("daily", START, END, [folder], OWNERS, CT, APP)
    assert texts(daily)[1:3] == ["*<@U1>* — 2 bids", "• 🇧🇷 Ana Silva — 2 bids"]
    assert texts(daily)[3].startswith("*Total: 2 bids · 1 bidder with bids*")


def test_no_buttons_without_an_app_url():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 2, 1)], OWNERS, CT, "",
                                my_bids={str(MARIA): {"Solo": 1}})
    assert accessories(messages) == [] and not any("accessory" in b for m in messages for b in m)
    _, messages = format_report("weekly", START, END, [rec(PEDRO, "Ana", 2, 1)], OWNERS, CT, "app.example")
    assert accessories(messages) == []


def test_missing_owner_falls_back():
    stranger = PydanticObjectId()
    _, messages = format_report("daily", START, END, [row(stranger, "Lee", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[1:3] == ["*Unknown owner* — 3 bids", "• Lee — 3 bids"]


def test_large_reports_respect_slack_limits():
    owners = {str(o): Owner(f"Owner {i}", f"Owner {i}") for i, o in enumerate(PydanticObjectId() for _ in range(60))}
    # 5 bidders × ~620 chars per owner forces the 3,000-char section split; 60 owners force a 2nd message
    rows = [row(PydanticObjectId(o), f"Bidder {i:03d} " + "x" * 600, i + 1)
            for i, o in enumerate(list(owners) * 5)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://a")
    assert len(messages) > 1 and all(len(m) <= 50 for m in messages)
    assert all(len(s) <= 3000 for s in texts(messages))
    ids = [b["accessory"]["action_id"] for m in messages for b in m if "accessory" in b]
    assert len(ids) == len(set(ids)) == 60  # every action_id in the post is unique
    for m in messages:  # a header is never left at the end of a message, away from its body
        assert "accessory" not in m[-1]
    body = "\n".join(texts(messages))
    assert all(body.count(f"Bidder {i:03d} ") == 1 for i in range(300))
    assert "(cont.)" in body and len(accessories(messages)) == 60  # one button per owner, only on the header


def test_user_text_is_escaped_but_slack_mentions_are_kept():
    hostile = "Ana <!channel> & co"
    owners = {**OWNERS, str(MARIA): Owner("maria", "<https://evil.example|Maria>")}
    rows = [row(PEDRO, hostile, 2, profile="<https://evil.example|click>"),
            row(PEDRO, "Bad <!here>", None, error="AccessDenied"),
            row(MARIA, "Mia", 1)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://app.example")
    t = texts(messages)
    assert t[1:3] == ["*&lt;https://evil.example|Maria&gt;* — 1 bid", "• Mia — 1 bid"]
    assert t[3] == "*<@U1>* — 2 bids · 1 failed"
    assert t[4] == ("• Ana &lt;!channel&gt; &amp; co · &lt;https://evil.example|click&gt; — 2 bids\n"
                    "• ⚠️ Bad &lt;!here&gt; — couldn't read screenshot folder")
    assert "<!" not in "".join(t)


def test_daily_footer_says_bidder_for_one_and_bidders_for_more():
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 3 bids · 1 bidder with bids*")
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3), row(MARIA, "Mia", 1)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 4 bids · 2 bidders with bids*")


def test_refreshed_report_footer_says_when_it_was_updated():
    updated = datetime(2026, 10, 3, 14, 5, tzinfo=timezone.utc)  # Sat 9:05 AM CDT
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a", updated_at=updated)
    assert texts(messages)[-1] == "*Total: 3 bids · 1 bidder with bids* · <https://a/bidders|View in app> · updated Sat 9:05 AM"


def test_owner_is_a_header_with_the_button_and_a_full_width_body():
    rows = [rec(PEDRO, "Ana Silva", 160, 12), rec(PEDRO, "Raj", 3, 0)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP, my_bids={str(PEDRO): {"Cory": 2}})
    blocks = messages[0]
    assert [("accessory" in b) for b in blocks] == [False, True, False, False]  # title, header, body, footer
    assert blocks[1]["text"]["text"] == "*<@U1>* — 163 approved · 12 pending"
    assert blocks[2]["text"]["text"] == ("• Ana Silva — 160 approved · 12 pending\n• Raj — 3 approved · 0 pending\n"
                                         "_My bids · 2 — Cory 2_")


def test_my_bids_only_owner_still_gets_a_header_and_a_body():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP,
                                my_bids={str(MARIA): {"Solo": 5}})
    maria = messages[0][1:3]
    assert [b["text"]["text"] for b in maria] == ["*Maria*", "_My bids · 5 — Solo 5_"]
    assert "accessory" in maria[0] and "accessory" not in maria[1]


def test_continuation_bodies_have_no_accessory():
    rows = [row(PEDRO, f"Bidder {i} " + "x" * 600, 10 - i) for i in range(8)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    blocks = messages[0][1:-1]
    assert len(blocks) > 2 and [("accessory" in b) for b in blocks] == [True] + [False] * (len(blocks) - 1)
    assert not blocks[1]["text"]["text"].startswith("*")
    assert blocks[2]["text"]["text"].split("\n")[0] == "*<@U1>* — 52 bids (cont.)"


def test_short_profile_names():
    assert _short_profile("Brazil | Fabricio Suzuki(Senior Data Engineer)") == "Fabricio Suzuki"
    assert _short_profile("Brazil | Lucas Saito(Senior Software Engineer)") == "Lucas Saito"
    assert _short_profile("A | B | Carla (QA)") == "Carla"  # text after the LAST "|"
    assert _short_profile("Andre Ballard") == "Andre Ballard"
    assert _short_profile("(x)") == "(x)"  # nothing left: the full name
    assert _short_profile("Brazil | (x)") == "Brazil | (x)"
    assert _short_profile("Brazil |") == "Brazil |"


def test_my_bids_line_uses_short_names_but_sorts_by_full_name():
    my = {str(PEDRO): {"brazil | Zed(QA)": 1, "Andre Ballard": 2, "Brazil | Cory(Dev)": 0}}
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP, my_bids=my)
    assert texts(messages)[2].endswith("\n_My bids · 3 — Andre Ballard 2 · Zed 1_")
