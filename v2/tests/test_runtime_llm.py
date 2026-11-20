"""Real conversations against Ollama (config.LLM_URL, gemma4:26b). Slow; marked `llm`.

1. The engine with the fake shop gateway of runtime_fakes: needs nothing but Ollama.
2. The assembled runtime (definition + tools + trace modules) with the real shop-support
   definition, if those modules can be imported; skipped otherwise.
3. The real server process on port 7881 with shop-support, same condition.

Run with -s to see the transcripts. They are also written to $AGENTKIT_DATA_DIR/runtime_llm_*.txt.
"""
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time

import httpx
import pytest

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR to a temp dir before agentkit.config is read)
from agentkit import config
from agentkit.llm import LLM
from agentkit.runtime import Engine, TextConversation
from runtime_fakes import FakeRecorder, MiniShop, make_definition

pytestmark = pytest.mark.llm
MODEL = "gemma4:26b"
PORT = 7881


def ollama_ready():
    try:
        tags = httpx.get(config.LLM_URL.rstrip("/") + "/api/tags", timeout=3).json()
        return any(model.get("name") == MODEL for model in tags.get("models", []))
    except Exception:
        return False


needs_ollama = pytest.mark.skipif(not ollama_ready(), reason=f"Ollama with {MODEL} is not reachable at {config.LLM_URL}")


def real_modules():
    try:
        import agentkit.definition  # noqa: F401
        import agentkit.tools  # noqa: F401
        import agentkit.trace  # noqa: F401
        from agentkit.definition import DefinitionStore
        DefinitionStore().load("shop-support")
        return True
    except Exception:
        return False


class Customer:
    """A customer who answers what is asked: scripted by rule, so the test does not need a second model."""

    def __init__(self, order_id, phone_last4, opening="안녕하세요, 주문한 물건을 환불하고 싶어요."):
        self.order_id, self.phone_last4, self.opening = order_id, phone_last4, opening
        self.identified = False

    def next(self, agent_text):
        if agent_text is None:
            return self.opening
        if re.search(r"접수할까요|접수해도|진행할까요|진행해도|하시겠|맞으신가요|맞으실까요|동의", agent_text):
            return "네, 접수해 주세요."
        if not self.identified:
            self.identified = True
            return f"주문번호는 {self.order_id}이고 휴대폰 뒤 네 자리는 {self.phone_last4}예요."
        if re.search(r"사유|이유", agent_text):
            return "단순 변심이에요. 환불해 주세요."
        if re.search(r"주문\s?번호|뒤\s?(4|네)\s?자리", agent_text):
            return f"주문번호 {self.order_id}, 휴대폰 뒤 네 자리 {self.phone_last4}입니다."
        return "네, 그 주문 환불해 주세요. 사유는 단순 변심이에요."


async def converse(driver, customer, finished, max_turns=8):
    """Returns the transcript as lines; stops when finished(turns) is true."""
    lines, turns, agent_text = [], [], None
    for _ in range(max_turns):
        user_text = customer.next(agent_text)
        turn = await driver.say(user_text)
        turns.append(turn)
        agent_text = turn.text
        lines.append(f"고객: {user_text}")
        for call in turn.tool_calls:
            lines.append(f"   [도구 {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)}) -> ok={call['ok']} "
                         f"executed={call['executed']} needs_confirmation={call.get('needs_confirmation')} "
                         f"error={call['error_type']}]")
        lines.append(f"상담: {turn.text}   ({turn.ms:.0f} ms{', 넘김' if turn.handoff else ''})")
        if finished(turns):
            break
    return lines, turns


def show(name, lines):
    transcript = "\n".join(lines)
    print(f"\n----- {name} -----\n{transcript}\n")
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / f"runtime_llm_{name}.txt").write_text(transcript + "\n", encoding="utf-8")
    except OSError:
        pass
    return transcript


def executed(turns, name):
    return [(index, call) for index, turn in enumerate(turns) for call in turn.tool_calls
            if call["name"] == name and call["executed"]]


@needs_ollama
async def test_llm_refund_with_fake_shop():
    """Verification -> lookup -> the refund is proposed -> consent -> executed once, never before the consent."""
    shop, recorder = MiniShop(), FakeRecorder()
    definition = make_definition(guards=[{
        "id": "no_fake_refund", "kind": "claims_lookup", "unless_tools": ["request_refund"],
        "pattern": r"환불\S{0,3}\s?(접수|처리)\S{0,2}\s?(됐|되었|완료|했습니다|했어요)",
        "nudge": "(시스템 안내: request_refund를 호출하지 않고 환불이 접수된 것처럼 말하려 했습니다. "
                 "고객이 동의했다면 같은 인자로 request_refund를 호출하세요.)"}])
    driver = TextConversation(Engine(definition, shop, recorder, LLM(model=MODEL), test=True, simulated=True))
    lines, turns = await converse(driver, Customer("A1001", "1234"), lambda turns: bool(executed(turns, "request_refund")))
    await driver.close()
    transcript = show("fake_shop_refund", lines)

    assert shop.refunds == [(driver.session_id, "A1001")], transcript            # executed exactly once
    names = [name for _, name, _ in shop.calls]
    assert "verify_customer" in names and names.index("verify_customer") < names.index("request_refund")
    proposals = [(i, c) for i, t in enumerate(turns) for c in t.tool_calls
                 if c["name"] == "request_refund" and c.get("needs_confirmation")]
    assert proposals, transcript
    proposed_at, executed_at = proposals[0][0], executed(turns, "request_refund")[0][0]
    assert executed_at > proposed_at, transcript                                  # consent came in between
    # the confirmation sentence reached the customer: product and amount were read out
    assert "30000" in turns[proposed_at].text.replace(",", "") and "이어폰" in turns[proposed_at].text, transcript
    # nothing about the order was said before the verification succeeded
    verified_at = next(i for i, t in enumerate(turns) for c in t.tool_calls if c["name"] == "verify_customer" and c["ok"])
    assert not any("이어폰" in t.text or "30000" in t.text for t in turns[:verified_at]), transcript
    assert all(t.text for t in turns) and not driver.handed_off
    assert len(recorder.last_turns()) == len(turns)


@needs_ollama
async def test_llm_large_refund_is_handed_off_with_fake_shop():
    shop, recorder = MiniShop(), FakeRecorder()
    driver = TextConversation(Engine(make_definition(), shop, recorder, LLM(model=MODEL), test=True, simulated=True))
    lines, turns = await converse(driver, Customer("A1002", "5678"), lambda turns: turns[-1].handoff)
    transcript = show("fake_shop_handoff", lines)
    assert driver.handed_off and shop.handoffs and not shop.refunds, transcript
    after = await driver.say("그럼 언제 연락 와요?")
    assert after.text == "상담원에게 연결되었습니다. 잠시만 기다려 주세요." and after.handoff
    await driver.close()
    assert recorder.ended == [(driver.session_id, "handoff")]
    assert recorder.of("handoff")[0].attrs["rule_id"] == "large_refund", recorder.of("handoff")[0].attrs


@needs_ollama
@pytest.mark.skipif(not real_modules(), reason="definition/tools/trace modules or shop-support are not there yet")
async def test_llm_shop_support_with_real_modules():
    """PLAN WP4: shop-support in text: 본인 확인 -> 주문 조회 -> 환불 제안 -> 동의 -> 접수."""
    driver = await TextConversation.open("shop-support", test=True, simulated=True, label="wp4-llm-refund")
    lines, turns = await converse(driver, Customer("A1001", "1234"), lambda turns: bool(executed(turns, "request_refund")))
    session_id = driver.session_id
    await driver.close()
    transcript = show("shop_support_refund", lines)
    done = executed(turns, "request_refund")
    assert len(done) == 1, transcript
    proposals = [i for i, t in enumerate(turns) for c in t.tool_calls
                 if c["name"] == "request_refund" and c.get("needs_confirmation")]
    assert proposals and proposals[0] < done[0][0], transcript
    assert "32000" in turns[proposals[0]].text.replace(",", ""), transcript
    verified_at = next(i for i, t in enumerate(turns) for c in t.tool_calls if c["name"] == "verify_customer" and c["ok"])
    assert not any("이어폰" in t.text or "32000" in t.text for t in turns[:verified_at]), transcript

    # the trace module stored the conversation
    from agentkit.trace import get_recorder, get_store
    get_recorder().flush(timeout=5.0)
    stored = get_store().get_session(session_id)
    assert stored and len(stored["turns"]) == len(turns), stored and stored.get("turns")
    kinds = {span["kind"] if isinstance(span, dict) else span.kind for span in stored["spans"]}
    assert {"llm", "tool", "output"} <= kinds, kinds


@needs_ollama
@pytest.mark.skipif(not real_modules(), reason="definition/tools/trace modules or shop-support are not there yet")
async def test_llm_real_server_three_text_sessions():
    """python -m agentkit.server on 7881: three shop-support text conversations at once."""
    import websockets
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.skip(f"port {PORT} is in use")
    env = {**os.environ, "AGENTKIT_DATA_DIR": str(config.DATA_DIR)}
    process = subprocess.Popen([sys.executable, "-m", "agentkit.server", "--port", str(PORT)],
                               cwd=str(config.V2_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=1).json()
                break
            except Exception:
                assert process.poll() is None, process.stdout.read().decode(errors="replace")[-2000:]
                await asyncio.sleep(0.3)
        else:
            pytest.fail("the server did not come up")
        assert "agentkit.definition.api" in health["routers"], health

        async def one(index):
            url = f"ws://127.0.0.1:{PORT}/ws?agent=shop-support&channel=text&test=1"
            async with websockets.connect(url) as ws:
                ready = json.loads(await ws.recv())
                assert ready["type"] == "ready" and ready["agent"] == "shop-support", ready
                await ws.send(json.dumps({"type": "chat", "id": f"c{index}", "text": "안녕하세요, 배송이 언제 오는지 궁금해요."}))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    assert message["type"] != "error", message
                    if message["type"] == "chat_done":
                        return ready["session_id"], message["text"]

        results = await asyncio.gather(*(one(i) for i in range(3)))
        lines = [f"세션 {session}: {reply}" for session, reply in results]
        transcript = show("server_three_sessions", lines)
        assert len({session for session, _ in results}) == 3 and all(reply for _, reply in results), transcript
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", PORT)) != 0
