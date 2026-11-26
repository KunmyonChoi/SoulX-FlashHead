"""Going through mail one at a time and rewording a reply by voice (office-assistant v14), with the real gateway
and the fake Google: step words run without the model, one mail is put away without a confirmation and can be
brought back, and a reworded mail replaces the one that was waiting."""
import asyncio

import runtime_fakes  # noqa: F401

from agentkit.contracts import ToolContext
from agentkit.runtime import Engine
from agentkit.tools import build_gateway
from agentkit.tools import google
from runtime_fakes import FakeRecorder, ScriptedLLM, gather, fixed_now, make_definition, text, tool

ACTIONS = [
    {"name": "next_mail", "tool": "next_mail", "say": "화면에서 '다음'을 눌렀습니다", "phrases": ["다음", "넘겨 줘"],
     "while_showing": "mail"},
    {"name": "archive_next", "tool": "triage_mail", "say": "화면에서 '보관'을 눌렀습니다", "phrases": ["보관", "보관해 줘"],
     "arguments": {"action": "archive"}, "while_showing": "mail"},
]
TOOLS = ["list_mail", "read_mail", "next_mail", "triage_mail", "undo_cleanup", "send_mail"]


def office(scripts):
    definition = make_definition(tool_packs=["google"], tools=TOOLS, actions=ACTIONS)
    llm = ScriptedLLM(scripts)
    engine = Engine(definition, build_gateway(), FakeRecorder(), llm, now=fixed_now, simulated=True)
    return engine, llm, google._fakes.of({"session_id": engine.session_id})


def done(events):
    return [(e.data["name"], bool(e.data.get("executed"))) for e in events if e.type == "tool" and e.data.get("state") == "done"]


def shown(events, kind="mail"):
    return [e.data["data"] for e in events if e.type == "display" and e.data["kind"] == kind]


def ctx(turn, said="", s="tri"):
    return ToolContext(session_id=s, agent_id="office-assistant", version=14, turn=turn, last_user_text=said, simulated=True, test=True)


async def test_step_words_run_the_tool_before_the_model_and_only_over_an_open_mail():
    engine, llm, fake = office([[tool("list_mail", filter="unread")], [text("세 통 있어요.")],      # "메일 보여 줘"
                                [text("다음 장은 없어요.")],                                        # "다음" over the list: the model's
                                [tool("next_mail")], [text("김민수 님 메일이에요.")],                # "하나씩 보자"
                                [text("이서연 님 메일이에요.")],                                    # "다음": one pass, no tools
                                [text("보관했어요. 보안팀 메일은 의심스러워요.")]])                  # "보관"
    await gather(engine.reply("메일 보여 줘"))
    over_list = await gather(engine.reply("다음"))
    assert done(over_list) == []                                  # a list is showing, not a mail: no shortcut
    await gather(engine.reply("하나씩 보자"))
    passes = len(llm.calls)
    stepped = await gather(engine.reply("다음."))
    assert done(stepped) == [("next_mail", False)] and shown(stepped)[0]["ref"] == 2
    assert len(llm.calls) == passes + 1 and not llm.calls[-1]["tools"]            # one pass, to say it
    assert [m for m in llm.calls[-1]["messages"] if m["role"] == "user"][-1]["content"].endswith("다음.")   # their own words
    put_away = await gather(engine.reply("보관해 줘"))
    assert done(put_away) == [("triage_mail", True)]
    mail = shown(put_away)[0]
    assert mail["ref"] == 3 and mail["done"]["ref"] == 2 and mail["done"]["action"] == "archive"
    assert [m["box"] for m in fake.mails if m["id"] == "m2"] == ["archive"]


async def test_a_step_word_does_not_answer_a_waiting_confirmation():
    engine, llm, fake = office([[tool("list_mail", filter="unread")], [text("세 통 있어요.")],
                                [tool("next_mail")], [text("김민수 님 메일이에요.")],
                                [tool("send_mail", reply_to="1", body="잘 받았습니다.")], [text("보낼까요?")],
                                [text("아직 보내지 않았어요. 보낼까요?")]])
    await gather(engine.reply("메일 보여 줘"))
    await gather(engine.reply("하나씩 보자"))
    await gather(engine.reply("답장해 줘, 잘 받았다고"))
    events = await gather(engine.reply("다음"))
    assert ("next_mail", False) not in done(events) and fake.sent == []


async def test_one_mail_at_a_time_to_the_end_and_back():
    gateway = build_gateway()
    await gateway.call(ctx(1), "google.list_mail", {"filter": "unread"})
    first = await gateway.call(ctx(2), "google.next_mail", {})
    assert first.display[0]["data"]["position"] == {"name": "읽지 않은 메일", "index": 1, "total": 3, "left": 2}
    nothing_open = await gateway.call(ctx(2, s="tri-b"), "google.triage_mail", {"action": "archive"})
    assert not nothing_open.ok and nothing_open.error_type == "precondition_failed"
    away = await gateway.call(ctx(3), "google.triage_mail", {"action": "archive"})
    assert away.executed and not away.needs_confirmation and "1번 김민수 메일을 보관했습니다" in away.text
    again = await gateway.call(ctx(4), "google.triage_mail", {"action": "archive"})        # the same words, the next mail
    assert again.executed and again.display[0]["data"]["done"]["ref"] == 2
    refused = await gateway.call(ctx(5), "google.triage_mail", {"action": "spam"})
    assert not refused.ok and refused.error_type == "invalid_input"                           # spam keeps its confirmation (cleanup_mail)
    end = await gateway.call(ctx(6), "google.next_mail", {})
    assert end.ok and end.display[0]["data"]["finished"] and "모두 보았습니다" in end.text
    fake = google._fakes.of(ctx(1))
    assert [m["box"] for m in fake.mails[:2]] == ["archive", "archive"]
    back = await gateway.call(ctx(7, "되돌려"), "google.undo_cleanup", {})                     # the newest: mail 2
    assert back.executed and [m["box"] for m in fake.mails[:2]] == ["archive", "inbox"]


async def test_a_reworded_mail_replaces_the_one_that_was_waiting(monkeypatch):
    monkeypatch.setattr(google, "SEND_DELAY", 0.05)
    gateway, s = build_gateway(), "tri-c"
    await gateway.call(ctx(1, s=s), "google.list_mail", {"filter": "unread"})
    first = await gateway.call(ctx(2, s=s), "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다. 금요일까지 의견 드리겠습니다."})
    assert first.needs_confirmation and first.display[0]["data"]["compose"]["before"] is None
    unclear = await gateway.call(ctx(3, "금요일을 목요일로 바꿔 줘", s=s), "google.send_mail",
                                 {"reply_to": "1", "body": "잘 받았습니다. 금요일까지 의견 드리겠습니다."})
    assert not unclear.ok and not unclear.executed
    same = await gateway.call(ctx(3, "금요일을 목요일로 바꿔 줘", s=s), "google.send_mail",
                              {"reply_to": "1", "body": "잘 받았습니다. 금요일까지 의견 드리겠습니다."})
    assert not same.ok and "똑같아서" in same.text                                          # told to really change it
    # the model names only the new body: the recipient and subject of the mail being written are kept
    new = await gateway.call(ctx(3, "금요일을 목요일로 바꿔 줘", s=s), "google.send_mail", {"body": "잘 받았습니다. 목요일까지 의견 드리겠습니다."})
    assert new.needs_confirmation and new.confirmation_text == "김민수에게 보낼 메일에서 바뀐 부분은 '목요일까지 의견 드리겠습니다.'입니다. 이대로 보낼까요?"
    card = new.display[0]["data"]["compose"]
    assert card["before"] == "잘 받았습니다. 금요일까지 의견 드리겠습니다." and card["reply_to"] == "1"
    assert [p["arguments"]["body"] for p in gateway.pending(s)] == ["잘 받았습니다. 목요일까지 의견 드리겠습니다."]   # one waits, the new one
    old = await gateway.call(ctx(4, "응, 보내", s=s), "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다. 금요일까지 의견 드리겠습니다."})
    assert not old.executed                                    # the first wording can no longer be sent by a "응"
    sent = await gateway.call(ctx(5, "응, 보내", s=s), "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다. 목요일까지 의견 드리겠습니다."})
    assert not sent.executed and sent.needs_confirmation       # asked anew after the stray call; then it goes
    sent = await gateway.call(ctx(6, "응, 보내", s=s), "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다. 목요일까지 의견 드리겠습니다."})
    assert sent.executed
    await asyncio.sleep(0.2)
    assert [d["body"] for d in google._fakes.of(ctx(1, s=s)).sent] == ["잘 받았습니다. 목요일까지 의견 드리겠습니다."]


async def test_the_engine_lets_a_revisable_request_be_called_again():
    engine, llm, fake = office([[tool("list_mail", filter="unread")], [text("세 통 있어요.")],
                                [tool("send_mail", reply_to="1", body="잘 받았습니다. 금요일까지 드리겠습니다.")], [text("보낼까요?")],
                                [tool("send_mail", reply_to="1", body="잘 받았습니다. 목요일까지 드리겠습니다.")], [text("이대로 보낼까요?")]])
    await gather(engine.reply("메일 보여 줘"))
    await gather(engine.reply("1번에 답장해 줘"))
    events = await gather(engine.reply("금요일을 목요일로 고쳐 줘"))
    told = [m for m in llm.calls[4]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "고친 인자로 send_mail 도구를 지금 다시 호출" in told and llm.calls[4]["tools"]
    assert shown(events, "confirm")[-1]["compose"]["before"] == "잘 받았습니다. 금요일까지 드리겠습니다."
    assert "목요일까지 드리겠습니다" in events[-1].text and "금요일" not in events[-1].text


async def test_history_gives_way_before_the_context_is_full():
    scripts = [[text("세 통 있어요.")]] * 12
    definition = make_definition(tool_packs=["google"], tools=TOOLS, actions=ACTIONS)
    definition.llm.num_ctx = 8192
    engine = Engine(definition, build_gateway(), FakeRecorder(), ScriptedLLM(scripts), now=fixed_now, simulated=True)
    engine._opening_entries = [{"role": "user", "content": "(대화가 시작되었습니다.)"}, {"role": "tool", "content": "메뉴 " * 400}]
    engine.history = list(engine._opening_entries)
    for i in range(6):
        await gather(engine.reply(f"질문 {i} " + "가" * 600))
    assert len(engine.history) == 2 + 12
    engine._prompt_tokens = 7000                       # room left: nothing goes
    await gather(engine.reply("질문 6"))
    assert len(engine.history) == 2 + 14
    engine._prompt_tokens = 7900                       # within CONTEXT_ROOM of 8192: the oldest exchanges go
    await gather(engine.reply("질문 7"))
    said = [m["content"] for m in engine.history if m["role"] == "user"]
    assert said[0].startswith("(대화가 시작") and "질문 0" not in " ".join(said) and said[-1] == "질문 7"
    engine._prompt_tokens = 9000                       # still too much with two exchanges left: the opening goes
    await gather(engine.reply("질문 8"))
    said = [m["content"] for m in engine.history if m["role"] == "user"]
    assert not said[0].startswith("(대화가 시작") and said[-1] == "질문 8" and len(said) >= 2
