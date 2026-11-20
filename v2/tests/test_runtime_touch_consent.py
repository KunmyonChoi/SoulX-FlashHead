"""Touches that propose or consent (ClientAction.propose / .consent), with the real gateway and the fake Google."""
import asyncio

import runtime_fakes  # noqa: F401

from agentkit.runtime import Engine
from agentkit.tools import build_gateway
from agentkit.tools import google
from runtime_fakes import FakeRecorder, ScriptedLLM, gather, fixed_now, make_definition, text, tool

ACTIONS = [
    {"name": "show_mail", "tool": "list_mail", "say": "화면에서 메일 목록을 열었습니다"},
    {"name": "plan_cleanup", "tool": "cleanup_mail", "propose": True, "say": "화면에서 정리할 메일을 골랐습니다"},
    {"name": "confirm_cleanup", "tool": "cleanup_mail", "consent": True, "say": "화면에서 '이대로 정리'를 눌렀습니다"},
    {"name": "send", "tool": "send_mail", "consent": True, "say": "화면에서 '보내기'를 눌렀습니다"},
    {"name": "send_unmarked", "tool": "send_mail", "say": "화면에서 보내기를 눌렀습니다"},
]


def office(scripts):
    definition = make_definition(tool_packs=["google"], tools=["list_mail", "cleanup_mail", "send_mail", "mail_briefing"],
                                 actions=ACTIONS)
    llm = ScriptedLLM(scripts)
    engine = Engine(definition, build_gateway(), FakeRecorder(), llm, now=fixed_now, simulated=True)
    return engine, llm, google._fakes.of({"session_id": engine.session_id})


def tools_done(events):
    return [(e.data["name"], e.data.get("executed"), e.data.get("needs_confirmation")) for e in events
            if e.type == "tool" and e.data.get("state") == "done"]


async def test_a_send_button_marked_consent_sends_at_once(monkeypatch):
    monkeypatch.setattr(google, "SEND_DELAY", 0.05)
    engine, llm, fake = office([[text("메일을 정리해 보여 드렸어요.")], [text("보낼게요.")]])
    await gather(engine.act("show_mail", {"filter": "unread"}))
    events = await gather(engine.act("send", {"reply_to": "1", "body": "잘 받았습니다."}))
    assert tools_done(events) == [("send_mail", True, False)]
    await asyncio.sleep(0.2)
    assert len(fake.sent) == 1


async def test_an_unmarked_action_on_a_consent_tool_is_refused():
    engine, llm, fake = office([])
    events = await gather(engine.act("send_unmarked", {"to": "a@b.c", "subject": "x", "body": "y"}))
    assert events[-1].type == "done" and "spoken consent" in events[-1].data["error"]


async def test_plan_then_confirm_cleanup_by_touch():
    engine, llm, fake = office([[text("광고예요.")], [text("화면의 계획을 확인하고 눌러 주세요.")], [text("정리했어요.")]])
    await gather(engine.act("show_mail", {"filter": "ads"}))
    plan = await gather(engine.act("plan_cleanup", {"spam": "1,2"}))
    assert tools_done(plan) == [("cleanup_mail", False, True)]
    card = [e.data for e in plan if e.type == "display"][0]
    assert card["kind"] == "confirm" and card["data"]["arguments"] == {"spam": "1,2"} and len(card["data"]["plan"][0]["items"]) == 2
    told = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "아직 실행하지 않았습니다" in told                                        # not "already done"
    assert await fake.counts() == {"unread": 3, "ads": 2, "all": 7}
    done = await gather(engine.act("confirm_cleanup", {"spam": "1"}))                 # the user unticked one
    assert tools_done(done) == [("cleanup_mail", True, False)]
    assert await fake.counts() == {"unread": 3, "ads": 1, "all": 6}


async def test_touch_consent_does_not_reach_the_models_own_calls_in_that_turn():
    # after a touch the model replies without tools, so nothing it does can use the touch's consent
    engine, llm, fake = office([[text("목록이에요.")], [text("정리했어요.")]])
    await gather(engine.act("show_mail", {"filter": "unread"}))
    await gather(engine.act("confirm_cleanup", {"archive": "1"}))
    assert not llm.calls[-1]["tools"]
    events = await gather(engine.reply("그럼 김민수 님께 잘 받았다고 보내 줘"))      # the next, spoken turn: proposes only
    assert all(not d[1] for d in tools_done(events) if d[0] == "send_mail") and fake.sent == []
