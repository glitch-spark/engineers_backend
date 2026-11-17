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


def code_lines(body):
    """The table lines of one body section: inside the ``` fences, column header included."""
    inner = body.split("```")[1]
    return inner.strip("\n").split("\n")


DAILY_HEAD = "Bidder              Bids  Appr  Pend   Rej"
DAILY_RULE = "─" * 42


def test_daily_post_is_a_table_per_owner():
    rows = [rec(PEDRO, "Ana Silva", 160, 12, country="BR", profile="AI Full-stack"), rec(PEDRO, "Raj Kumar", 0, 0),
            row(PEDRO, "John Doe", None, error="AccessDenied"), rec(PEDRO, "Bo", 3, 0, 2), rec(MARIA, "Mia Cruz", 0, 0)]
    my = {str(PEDRO): {"Cory": 23, "Brazil | Arda(QA)": 12}}
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, APP, my_bids=my)
    assert text == "Daily bids — Fri Oct 2: 163 approved · 12 pending · 2 rejected"  # fallback unchanged
    assert texts(messages) == [
        "📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT",
        "*<@U1>* — 177 bids · 163 approved · 12 pending · 2 rejected · 1 failed · My bids 35",
        "```\n"
        "Bidder              Bids  Appr  Pend   Rej\n"
        "Ana Silva            172   160    12     0\n"
        "Bo                     5     3     0     2\n"
        "John Doe            couldn't read folder\n"
        "──────────────────────────────────────────\n"
        "Bidders total        177   163    12     2\n"
        "My bids               35\n"
        "  Arda                12\n"
        "  Cory                23\n"
        "```",
        "*Total: 177 bids by 2 bidders · 163 approved · 12 pending · 2 rejected* · "
        "<https://app.example/bidders|View in app>",
    ]
    assert all(len(line) <= 42 for line in code_lines(texts(messages)[2]))
    assert "🇧🇷" not in "".join(texts(messages)) and "AI Full-stack" not in "".join(texts(messages))


def test_weekly_post_is_a_table_with_pay_and_open_pay():
    rows = [rec(PEDRO, "Ana Silva", 160, 12, rate=0.06), row(PEDRO, "Lee", 2), rec(MARIA, "Mia Cruz", 0, 0, rate=0.06)]
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, APP, my_bids={str(PEDRO): {"Cory": 4}})
    assert text == "Weekly bids & pay — week ending Fri Oct 2: 162 approved · 12 pending · $10.60"  # fallback unchanged
    assert texts(messages) == [
        "💰 *Weekly bids & pay — week ending Fri Oct 2* · Thu Oct 1 6:30 PM → Fri Oct 2 6:30 PM CDT",
        "*<@U1>* — 174 bids · 160 approved · 12 pending · *$10.60* · My bids 4",
        "```\n"
        "Bidder              Bids  Appr   Rate        Pay  Pend\n"
        "Ana Silva            172   160  $0.06      $9.60    12\n"
        "Lee                    2     —  $0.50      $1.00     —\n"
        "──────────────────────────────────────────────────────\n"
        "Bidders total        174   160            $10.60    12\n"
        "My bids                4\n"
        "  Cory                 4\n"
        "```",
        "*Total: 174 bids by 2 bidders · 160 approved · 12 pending · $10.60* · <https://app.example/bidders|View in app>",
    ]
    assert accessories(messages) == [
        {"type": "button", "text": {"type": "plain_text", "text": "Open Pay"},
         "url": f"https://app.example/bidders?section=pay&owner={PEDRO}&range=pay_week&date=2026-10-02",
         "action_id": f"bids-link-{PEDRO}"}]
    assert "accessory" not in messages[0][-1] and "accessory" not in messages[0][2]


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


def test_rejected_only_when_some_but_pending_always():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 5, 0)], OWNERS, CT, APP)
    t = texts(messages)
    assert t[1] == "*<@U1>* — 5 bids · 5 approved · 0 pending"
    assert t[3].startswith("*Total: 5 bids by 1 bidder · 5 approved · 0 pending* · ")
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 5, 0, 2)], OWNERS, CT, APP)
    assert texts(messages)[1] == "*<@U1>* — 7 bids · 5 approved · 0 pending · 2 rejected"


def test_no_bids_says_so():
    rows = [rec(PEDRO, "Ana", 0, 0), row(MARIA, "Mia", 0)]
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    assert texts(messages) == ["📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT", "No bids today.",
                               "<https://app.example/bidders|View in app>"]
    assert text.endswith(": no bids") and accessories(messages) == []
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, APP)
    assert texts(messages)[1] == "No bids this week." and text.endswith(": no bids")


def test_my_bids_never_count_in_a_total():
    rows = [rec(PEDRO, "Ana Silva", 160, 12)]
    _, plain = format_report("daily", START, END, rows, OWNERS, CT, APP)
    _, mine = format_report("daily", START, END, rows, OWNERS, CT, APP, my_bids={str(PEDRO): {"Cory": 50}})
    assert texts(mine)[-1] == texts(plain)[-1] == ("*Total: 172 bids by 1 bidder · 160 approved · 12 pending* · "
                                                   "<https://app.example/bidders|View in app>")
    assert "Bidders total        172   160    12     0" in code_lines(texts(mine)[2])
    assert texts(mine)[1].endswith(" · My bids 50")


def test_my_bids_only_owner_gets_a_header_and_only_the_my_bids_lines():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP,
                                my_bids={str(MARIA): {"Solo": 5, "Zero": 0}})
    maria = messages[0][1:3]
    assert [b["text"]["text"] for b in maria] == ["*Maria* — My bids 5", "```\nMy bids                5\n  Solo                 5\n```"]
    assert "accessory" in maria[0] and "accessory" not in maria[1]
    assert texts(messages)[-1].startswith("*Total: 1 bid by 1 bidder · 1 approved · 0 pending*")


def test_my_bids_names_are_escaped_sorted_and_unknown_owners_survive():
    stranger = str(PydanticObjectId())
    my = {str(PEDRO): {"zed": 1, "Bob <!channel>": 2, "alice": 3}, stranger: {"Solo": 1}, str(MARIA): {"Zero": 0}}
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP, my_bids=my)
    t = texts(messages)
    assert t[t.index("*Unknown owner* — My bids 1") + 1] == "```\nMy bids                1\n  Solo                 1\n```"
    assert code_lines(t[2])[-4:] == ["My bids                6", "  alice                3",
                                     "  Bob &lt;!channel&gt;       2", "  zed                  1"]
    assert not any("Maria" in x for x in t)  # zero My bids and no rows: no section


def test_my_bids_use_short_names_sorted_by_the_shown_name():
    my = {str(PEDRO): {"Brazil | Zed(QA)": 1, "Arda": 2, "brazil | bob(Dev)": 3, "Bob": 4, "Aaa | Bob": 5,
                       "Brazil | Cory(Dev)": 0}}
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 1, 0)], OWNERS, CT, APP, my_bids=my)
    # shown name first, case-insensitive; the full name breaks the tie between the three Bobs
    assert [line.split()[0] for line in code_lines(texts(messages)[2])[-5:]] == ["Arda", "Bob", "Bob", "bob", "Zed"]
    assert [line.split()[-1] for line in code_lines(texts(messages)[2])[-5:]] == ["2", "5", "4", "3", "1"]


def test_folder_rows_show_dashes():
    folder = row(PEDRO, "Ana Silva", 2, country="BR")
    _, daily = format_report("daily", START, END, [folder], OWNERS, CT, APP)
    t = texts(daily)
    assert t[1] == "*<@U1>* — 2 bids"
    assert code_lines(t[2]) == [DAILY_HEAD, "Ana Silva              2     —     —     —", DAILY_RULE,
                                "Bidders total          2     —     —     —"]
    assert t[3].startswith("*Total: 2 bids by 1 bidder* · ")
    _, weekly = format_report("weekly", START, END, [folder], OWNERS, CT, APP)
    t = texts(weekly)
    assert t[1] == "*<@U1>* — 2 bids · *$1.00*"
    assert code_lines(t[2])[1] == "Ana Silva              2     —  $0.50      $1.00     —"
    assert t[3].startswith("*Total: 2 bids by 1 bidder · $1.00* · ")


def test_error_rows_get_their_own_line_after_the_bidders():
    rows = [row(PEDRO, "Zoe", None, error="AccessDenied"), rec(PEDRO, "Ana", 1, 0)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    t = texts(messages)
    assert t[1] == "*<@U1>* — 1 bid · 1 approved · 0 pending · 1 failed"
    assert code_lines(t[2])[1:3] == ["Ana                    1     1     0     0", "Zoe                 couldn't read folder"]
    _, only_errors = format_report("daily", START, END, rows[:1], OWNERS, CT, APP)
    assert texts(only_errors)[1:3] == ["*<@U1>* — 1 failed", f"```\n{DAILY_HEAD}\nZoe                 couldn't read folder\n```"]


def test_long_names_are_cut_to_18_characters():
    rows = [rec(PEDRO, "Maximiliano Fernandez-Oliveira", 1, 0), rec(PEDRO, "Exactly18Character", 2, 0)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP,
                                my_bids={str(PEDRO): {"Brazil | Bartholomew Montgomery(QA)": 1}})
    lines = code_lines(texts(messages)[2])
    assert lines[1] == "Exactly18Character     2     2     0     0"
    assert lines[2] == "Maximiliano Ferna…     1     1     0     0"
    assert lines[-1] == "  Bartholomew Mon…     1"
    assert all(len(line) <= 42 for line in lines)


def test_no_buttons_without_an_app_url():
    _, messages = format_report("daily", START, END, [rec(PEDRO, "Ana", 2, 1)], OWNERS, CT, "",
                                my_bids={str(MARIA): {"Solo": 1}})
    assert accessories(messages) == [] and not any("accessory" in b for m in messages for b in m)
    _, messages = format_report("weekly", START, END, [rec(PEDRO, "Ana", 2, 1)], OWNERS, CT, "app.example")
    assert accessories(messages) == []


def test_missing_owner_falls_back():
    stranger = PydanticObjectId()
    _, messages = format_report("daily", START, END, [row(stranger, "Lee", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[1] == "*Unknown owner* — 3 bids"
    assert code_lines(texts(messages)[2])[1] == "Lee                    3     —     —     —"


def test_large_reports_respect_slack_limits():
    owners = {str(o): Owner(f"Owner {i}", f"Owner {i}") for i, o in enumerate(PydanticObjectId() for _ in range(60))}
    # 150 bidders per owner forces continuation code blocks; 60 owners force a 2nd message
    rows = [rec(PydanticObjectId(o), f"B{i:05d} & <long name> " + "x" * 30, i + 1, 1, 1)
            for i, o in enumerate(list(owners) * 150)]
    my = {o: {f"Profile {j}": 1 for j in range(20)} for o in owners}
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://a", my_bids=my)
    assert len(messages) > 1 and all(len(m) <= 50 for m in messages)
    assert all(len(s) <= 3000 for s in texts(messages))
    ids = [b["accessory"]["action_id"] for m in messages for b in m if "accessory" in b]
    assert len(ids) == len(set(ids)) == 60  # every action_id in the post is unique
    for m in messages:  # a header is never left at the end of a message, away from its body
        assert "accessory" not in m[-1]
    bodies = [s for s in texts(messages) if s.endswith("```")]
    assert len(bodies) > 60 and all(code_lines(b)[0] == DAILY_HEAD for b in bodies)  # header repeats in each
    assert all(b.startswith("```") or b.startswith("*Owner ") and " (cont.)\n```" in b for b in bodies)
    body = "\n".join(bodies)
    assert all(body.count(f"B{i:05d} &amp; &lt;") == 1 for i in range(9000))
    assert body.count("Bidders total") == body.count("\nMy bids ") == 60
    assert "<long" not in body


def test_user_text_is_escaped_but_slack_mentions_are_kept():
    hostile = "Ana <!channel> & co"
    owners = {**OWNERS, str(MARIA): Owner("maria", "<https://evil.example|Maria>")}
    rows = [row(PEDRO, hostile, 2, profile="<https://evil.example|click>"),
            row(PEDRO, "Bad <!here>", None, error="AccessDenied"),
            row(MARIA, "Mia `rm` ```", 1)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://app.example")
    t = texts(messages)
    assert t[1] == "*&lt;https://evil.example|Maria&gt;* — 1 bid"
    assert code_lines(t[2])[1] == "Mia 'rm' '''           1     —     —     —"  # backticks cannot close the block
    assert t[3] == "*<@U1>* — 2 bids · 1 failed"
    # padded on the shown text: "Ana <!channel> & co" (19) is cut to 18 before escaping
    assert code_lines(t[4])[1:3] == ["Ana &lt;!channel&gt; &amp; …     2     —     —     —",
                                     "Bad &lt;!here&gt;         couldn't read folder"]
    assert "<!" not in "".join(t) and "click" not in "".join(t)


def test_footer_says_bidder_for_one_and_bidders_for_more():
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 3 bids by 1 bidder*")
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3), row(MARIA, "Mia", 1)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 4 bids by 2 bidders*")


def test_refreshed_report_footer_says_when_it_was_updated():
    updated = datetime(2026, 10, 3, 14, 5, tzinfo=timezone.utc)  # Sat 9:05 AM CDT
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a", updated_at=updated)
    assert texts(messages)[-1] == "*Total: 3 bids by 1 bidder* · <https://a/bidders|View in app> · updated Sat 9:05 AM"


def test_owner_is_a_header_with_the_button_and_a_full_width_body():
    rows = [rec(PEDRO, "Ana Silva", 160, 12), rec(PEDRO, "Raj", 3, 0)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP, my_bids={str(PEDRO): {"Cory": 2}})
    blocks = messages[0]
    assert [("accessory" in b) for b in blocks] == [False, True, False, False]  # title, header, body, footer
    assert blocks[1]["text"]["text"] == "*<@U1>* — 175 bids · 163 approved · 12 pending · My bids 2"
    assert blocks[2]["text"]["text"].startswith("```\n") and blocks[2]["text"]["text"].endswith("\n```")


def test_continuation_bodies_repeat_the_column_header_and_have_no_accessory():
    rows = [rec(PEDRO, f"Bidder {i:03d}", 200 - i, 0) for i in range(150)]
    _, messages = format_report("daily", START, END, rows, OWNERS, CT, APP)
    blocks = messages[0][1:-1]
    assert len(blocks) > 2 and [("accessory" in b) for b in blocks] == [True] + [False] * (len(blocks) - 1)
    assert blocks[1]["text"]["text"].startswith(f"```\n{DAILY_HEAD}\nBidder 000 ")
    for b in blocks[2:]:
        assert b["text"]["text"].startswith(f"*<@U1>* (cont.)\n```\n{DAILY_HEAD}\n")
        assert len(b["text"]["text"]) <= 3000
    assert code_lines(blocks[-1]["text"]["text"])[-2:] == [DAILY_RULE, "Bidders total      18,825 18,825     0     0"]


def test_short_profile_names():
    assert _short_profile("Brazil | Fabricio Suzuki(Senior Data Engineer)") == "Fabricio Suzuki"
    assert _short_profile("Brazil | Lucas Saito(Senior Software Engineer)") == "Lucas Saito"
    assert _short_profile("A | B | Carla (QA)") == "Carla"  # text after the LAST "|"
    assert _short_profile("Andre Ballard") == "Andre Ballard"
    assert _short_profile("(x)") == "(x)"  # nothing left: the full name
    assert _short_profile("Brazil | (x)") == "Brazil | (x)"
    assert _short_profile("Brazil |") == "Brazil |"


