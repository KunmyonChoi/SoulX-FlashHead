"""The voice channel and the screen (docs/DISPLAY.md): the opening is spoken, screens are passed on,
a touch takes the turn. Scripted engine, fake TTS / STT / endpointer; no model, no network."""
import asyncio

from agentkit.runtime.voice import VoiceSession, serve_voice, voice_settings
from test_voice_session import AUDIO, CHUNK, GAP_BYTES, FakeSocket, Rig
from voice_fakes import Client, FakeEngine, FakeParts, FakeSTT, ev, fake_pcm, simple_script, until

OPENING = "어서 오세요. 화면의 메뉴를 보세요."
MENU = {"categories": [{"id": "coffee", "name": "커피", "items": [{"id": "americano", "name": "아메리카노", "price": 4500}]}]}
CART = {"lines": [{"line_id": "L1", "item_id": "americano", "name": "아메리카노", "quantity": 1, "line_total": 4500}],
        "total": 4500}
SCREEN = dict(opening={"say": OPENING, "tools": [{"name": "get_menu"}]}, screen={"panels": ["menu", "cart"]},
              actions=[{"name": "add_item", "tool": "add_to_cart", "description": "담기"}])


def opening_steps(wait=0.0):
    return [ev("tool", state="start", name="get_menu", arguments={}, effect="read"),
            ev("tool", state="done", name="get_menu", ok=True, effect="read"),
            ev("display", kind="menu", data=MENU, source="get_menu"), wait,
            ev("piece", OPENING), ev("done", OPENING, turn=0, opening=True)]


def touch_script(name, arguments):
    return [ev("tool", state="start", name="add_to_cart", arguments=arguments, effect="write"),
            ev("tool", state="done", name="add_to_cart", ok=True, effect="write", executed=True),
            ev("display", kind="cart", data=CART, source="get_cart"),
            ev("delta", "담았어요."), ev("piece", "담았어요."), ev("done", "담았어요.")]


class ScreenRig(Rig):
    """Rig with an engine that has an opening, a screen and a touch."""

    def __init__(self, script, parts=None, **overrides):
        self.engine = FakeEngine(script, **SCREEN)
        self.engine.opening_script = opening_steps()
        self.engine.act_script = touch_script
        self.client = Client()
        self.parts = parts or FakeParts()
        self.session = VoiceSession(self.engine, self.client.send_json, self.client.send_bytes,
                                    voice_settings(self.engine.definition, overrides), parts=self.parts,
                                    chunk_seconds=CHUNK)


def test_opening_is_spoken_on_connect_and_its_screen_is_forwarded():
    async def main():
        ws, engine = FakeSocket(), FakeEngine(simple_script("네."), **SCREEN)
        engine.opening_script = opening_steps()
        task = asyncio.create_task(serve_voice(ws, engine, "voice", {"interrupt_mode": "basic"}, parts=FakeParts()))
        expected = fake_pcm(OPENING) + b"\x00" * GAP_BYTES
        await until(lambda: sum(len(b) - 12 for b in ws.binary) >= len(expected))
        await asyncio.sleep(0.3)
        ws.disconnect()
        await asyncio.wait_for(task, 3)
        return ws, engine, expected

    ws, engine, expected = asyncio.run(main())
    ready = ws.json[0]
    assert ready["type"] == "ready" and ready["opening"] is True and ready["screen"] == {"panels": ["menu", "cart"]}
    assert ready["actions"] == [{"name": "add_item", "tool": "add_to_cart", "description": "담기"}]
    assert engine.opened == 1 and engine.turn == 0 and not engine.calls       # no customer turn was made up
    by_type = {}
    for message in ws.json[1:]:
        by_type.setdefault(message["type"], []).append(message)
    assert by_type["display"] == [{"type": "display", "id": "opening", "kind": "menu", "data": MENU,
                                   "source": "get_menu"}]
    assert [m["state"] for m in by_type["tool"]] == ["start", "done"] and by_type["tool"][0]["id"] == "opening"
    assert [m["text"] for m in by_type["chat_delta"]] == [OPENING]
    assert by_type["chat_done"] == [{"type": "chat_done", "id": "opening", "text": OPENING, "turn": 0}]
    order = [m["type"] for m in ws.json]
    assert order.index("display") < order.index("chat_delta") < order.index("chat_done")
    # ...and it was said aloud, as a reply
    assert b"".join(b[12:] for b in ws.binary) == expected
    # ("answer": the first sound after a tool call, as in any reply that looked something up)
    assert by_type["speech"][0]["phase"] == "answer" and by_type["speech"][0]["id"] == "opening"
    assert [phase for phase, _ in engine.outputs] == ["reply"]
    assert [(turn, role) for _, turn, role, _ in engine.recorder.audios] == [(0, "agent")]


def test_agent_without_an_opening_says_nothing_on_connect():
    async def main():
        ws, engine = FakeSocket(), FakeEngine(simple_script("네."))
        task = asyncio.create_task(serve_voice(ws, engine, "voice", {"interrupt_mode": "basic"}, parts=FakeParts()))
        await until(lambda: ws.json)
        await asyncio.sleep(0.3)
        ws.disconnect()
        await asyncio.wait_for(task, 3)
        return ws, engine

    ws, engine = asyncio.run(main())
    ready = ws.json[0]
    assert (ready["screen"], ready["actions"], ready["opening"]) == ({"panels": []}, [], False)
    assert engine.opened == 0 and not ws.binary
    assert [m["type"] for m in ws.json if m["type"] != "listen_state"] == ["ready"]


def test_display_event_of_a_reply_is_forwarded_and_not_spoken():
    def script(text):
        return [ev("tool", state="start", name="add_to_cart", arguments={}, effect="write"),
                ev("tool", state="done", name="add_to_cart", ok=True, effect="write", executed=True),
                ev("display", kind="cart", data=CART, source="get_cart"),
                ev("delta", "담았어요."), ev("piece", "담았어요."), ev("done", "담았어요.")]

    async def main():
        async with Rig(script) as rig:
            await rig.say("아메리카노 하나 주세요")
            await until(lambda: rig.session._reply_finished() and rig.client.of("chat_done"))
            return rig

    rig = asyncio.run(main())
    assert rig.client.of("display") == [{"type": "display", "id": "c1", "kind": "cart", "data": CART,
                                         "source": "get_cart"}]
    assert rig.client.pcm() == fake_pcm("담았어요.") + b"\x00" * GAP_BYTES
    assert [text for text, _, _ in rig.parts.tts.calls] == ["담았어요."]


def test_touch_cuts_the_speech_and_its_reply_is_spoken():
    long_opening = "어서 오세요. " + "메뉴를 천천히 보시고 편하게 말씀해 주세요. " * 4

    async def main():
        async with ScreenRig(simple_script("네."), interrupt_mode="basic") as rig:
            rig.engine.opening_script = [ev("piece", long_opening.strip()), ev("done", long_opening.strip())]
            assert rig.session.start_opening() is not None
            await until(lambda: len(rig.client.chunks()) >= 2)          # the opening is being said
            before = len(rig.client.pcm())
            await rig.session.on_message({"type": "action", "id": "a1", "name": "add_item",
                                          "arguments": {"item_id": "americano", "quantity": 1}})
            await until(lambda: rig.client.of("chat_done", id="a1") and rig.session._reply_finished())
            await asyncio.sleep(0.3)
            # a touch the agent does not know is answered with an error and cuts nothing
            generation = rig.session.generation
            await rig.session.on_message({"type": "action", "id": "a2", "name": "pay", "arguments": {}})
            assert rig.session.generation == generation
            return rig, before

    rig, before = asyncio.run(main())
    client, engine = rig.client, rig.engine
    assert engine.acts == [("add_item", {"item_id": "americano", "quantity": 1}, 1)]
    spoken = client.pcm()
    full_opening = sum(len(fake_pcm(s)) for s in long_opening.split(". ") if s)
    assert len(spoken) < full_opening                                    # the rest of the opening was dropped
    assert spoken.endswith(fake_pcm("담았어요.") + b"\x00" * GAP_BYTES)   # and the touch was answered aloud
    assert before <= len(spoken) - len(fake_pcm("담았어요.")) - GAP_BYTES + CHUNK * 32000
    assert client.of("display", id="a1") == [{"type": "display", "id": "a1", "kind": "cart", "data": CART,
                                              "source": "get_cart"}]
    assert client.of("tool", id="a1")[1]["executed"] is True
    assert client.of("chat_done", id="a1")[0]["text"] == "담았어요." and client.of("chat_done", id="a1")[0]["turn"] == 1
    refused = client.of("chat_done", id="a2")[0]
    assert refused["text"] == "" and "unknown action" in refused["error"]


def test_touch_in_unmute_mode_tells_the_browser_to_flush():
    async def main():
        async with ScreenRig(simple_script("주문하신 내역을 하나씩 천천히 읽어 드릴게요. " * 3), interrupt_mode="unmute") as rig:
            await rig.say("주문 내역 읽어 줘요")
            await until(lambda: len(rig.client.chunks()) >= 1)
            await rig.session.on_message({"type": "action", "name": "add_item", "arguments": {"item_id": "americano"}})
            assert rig.client.of("interrupt")                              # as for a spoken interruption
            await rig.session.on_message({"type": "played", "seq": 0, "offset": 0.1})
            await until(lambda: rig.engine.acts and rig.client.of("display"))
            await until(lambda: any(m.get("text") == "담았어요." for m in rig.client.of("chat_done")))
            return rig

    rig = asyncio.run(main())
    assert rig.engine.truncated and rig.client.of("chat_truncate")           # the cut reply was fixed up
    assert rig.engine.acts[0][:2] == ("add_item", {"item_id": "americano"})
    assert rig.client.of("chat_done")[-1]["id"].startswith("action-")        # an id is made up when none came


def test_engine_refusing_a_touch_is_reported_on_chat_done():
    async def main():
        async with ScreenRig(simple_script("네.")) as rig:
            rig.engine.act_script = lambda name, arguments: [ev("done", "", error="needs spoken consent")]
            await rig.session.on_message({"type": "action", "id": "a1", "name": "add_item", "arguments": {}})
            await until(lambda: rig.client.of("chat_done", id="a1"))
            return rig

    rig = asyncio.run(main())
    done = rig.client.of("chat_done", id="a1")[0]
    assert done["error"] == "needs spoken consent" and done["text"] == "" and not rig.client.binary


def test_display_of_a_speculative_reply_waits_for_the_commit():
    def script(text):
        return [ev("tool", state="start", name="get_cart", arguments={}, effect="read"),
                ev("tool", state="done", name="get_cart", ok=True, effect="read"),
                ev("display", kind="cart", data=CART, source="get_cart"), 0.2,
                ev("delta", "한 잔 있어요."), ev("piece", "한 잔 있어요."), ev("done", "한 잔 있어요.")]

    async def main():
        parts = FakeParts(stt=FakeSTT(["주문 내역 보여 줘요.", "주문 내역 보여 줘요. 그리고요.", "주문 내역 보여 줘요."]))
        async with Rig(script, parts=parts, speculative=True) as rig:
            client = rig.client
            await rig.mic(("start", None), in_speech=True)
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: len(rig.session.spec["outbox"]) >= 4)      # transcript, tool, tool, display
            assert not client.of("display")
            await rig.mic(("resume", None), in_speech=True)                # the speaker goes on: thrown away
            assert not client.of("display") and not client.of("tool")
            await rig.mic(("pause", AUDIO), in_speech=True)
            await until(lambda: len(rig.engine.calls) == 2 and len(rig.session.speech) > 0)
            assert not client.of("display")
            await rig.mic(("utterance", AUDIO), in_speech=False)
            await until(lambda: client.of("chat_done") and rig.session._reply_finished())
            return rig

    rig = asyncio.run(main())
    assert len(rig.client.of("display")) == 1 and rig.client.of("display")[0]["kind"] == "cart"
    order = [m["type"] for m in rig.client.json]
    assert order.index("transcript") < order.index("display") < order.index("chat_done")


def test_real_engine_opening_and_touch_through_the_voice_session():
    """The engine itself (scripted LLM, fake cafe) under the voice session: the two fit together."""
    import runtime_fakes
    from agentkit.runtime import Engine

    async def main():
        recorder, gateway = runtime_fakes.FakeRecorder(), runtime_fakes.MiniCafe()
        llm = runtime_fakes.ScriptedLLM([[runtime_fakes.text("바닐라 라떼 담았어요.")]])
        engine = Engine(runtime_fakes.make_cafe_definition(), gateway, recorder, llm, channel="voice")
        client = Client()
        session = VoiceSession(engine, client.send_json, client.send_bytes,
                               voice_settings(engine.definition, {"interrupt_mode": "unmute"}), parts=FakeParts(),
                               chunk_seconds=CHUNK)
        await session.prepare()
        task = asyncio.create_task(session.run())
        try:
            session.start_opening()
            await until(lambda: len(client.chunks()) >= 2)
            # touched while the opening is still being said: cut to what was heard, then carried out
            await session.on_message({"type": "action", "id": "a1", "name": "add_item",
                                      "arguments": {"item_id": "vanilla_latte", "quantity": 1}})
            await session.on_message({"type": "played", "seq": 1, "offset": 0.0})
            await until(lambda: client.of("chat_done", id="a1") and session._reply_finished())
        finally:
            await session.close()
            task.cancel()
            await asyncio.wait({task}, timeout=2)
        return engine, client, recorder, gateway

    engine, client, recorder, gateway = asyncio.run(main())
    assert [m["kind"] for m in client.of("display", id="opening")] == ["menu", "cart"]
    assert client.of("chat_done", id="opening")[0]["text"] == runtime_fakes.OPENING_WORDS
    assert client.of("interrupt") and client.of("chat_truncate", id="opening")
    heard = client.of("chat_truncate", id="opening")[0]["text"]
    assert heard and len(heard) < len(runtime_fakes.OPENING_WORDS)
    assert engine.history[4]["content"] == heard                         # the opening, cut to what was heard
    assert [m["role"] for m in engine.history[:4]] == ["user", "assistant", "tool", "tool"]   # the menu stays
    assert [m["kind"] for m in client.of("display", id="a1")] == ["cart"]
    assert client.of("chat_done", id="a1")[0]["text"] == "바닐라 라떼 담았어요."
    assert gateway.carts[engine.session_id] == {"vanilla_latte": 1} and engine.turn == 1
    turns = {r.turn: r for r in recorder.last_turns()}
    assert turns[0].interrupted and turns[0].agent_text == heard and turns[1].user_text.startswith("(화면에서")
    assert {s.turn for s in recorder.of("tts")} <= {0, 1}
