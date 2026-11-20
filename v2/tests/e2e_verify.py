"""End-to-end checks against a running v2 server (PLAN.md section 4). Not collected by pytest.

    python tests/e2e_verify.py [--base http://127.0.0.1:7871] [checks...]   e.g.  v1 v2 v5

Each check prints what it saw and ends with PASS or FAIL; the exit code is the number of failures.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import websockets

V2 = Path(__file__).resolve().parent.parent
results = {}


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=20) as response:
        return json.load(response)


def post(base, path, body=None):
    request = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Chat:
    """One text-channel conversation."""

    def __init__(self, base, agent, test=True):
        self.url = base.replace("http", "ws") + f"/ws?agent={agent}&channel=text&test={int(test)}"
        self.n = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None)
        self.ready = json.loads(await self.ws.recv())
        self.session_id = self.ready.get("session_id")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def say(self, text, quiet=False):
        self.n += 1
        started = time.perf_counter()
        await self.ws.send(json.dumps({"type": "chat", "id": f"t{self.n}", "text": text}))
        reply, tools, handoff, first = "", [], False, None
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), 90))
            kind = message.get("type")
            if kind == "chat_delta":
                first = first or time.perf_counter() - started
                reply += message.get("text", "")
            elif kind == "chat_replace":
                reply = message.get("text", "")
            elif kind == "tool" and message.get("state") == "done":
                tools.append({k: message.get(k) for k in
                              ("name", "ok", "executed", "needs_confirmation", "error_type")})
            elif kind == "handoff":
                handoff = True
            elif kind == "chat_done":
                reply = message.get("text") or reply
                break
            elif kind == "error":
                reply = "[error] " + str(message.get("message"))
                break
        if not quiet:
            print(f"   고객: {text}")
            for tool in tools:
                flags = [k for k in ("executed", "needs_confirmation") if tool[k]] + ([tool["error_type"]] if tool["error_type"] else [])
                print(f"      [도구 {tool['name']} {'ok' if tool['ok'] else '실패'} {' '.join(flags)}]")
            print(f"   상담: {reply}" + ("   (넘김)" if handoff else ""))
        return {"text": reply, "tools": tools, "handoff": handoff, "first_s": first, "total_s": time.perf_counter() - started}


def verdict(name, ok, note=""):
    results[name] = ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}\n")


async def check_v1(base):
    """Both agents come up from their definitions alone and answer a text turn."""
    agents = {a["id"]: a for a in get(base, "/api/agents")}
    ok = True
    for agent, question in (("tech-trend-anchor", "안녕하세요, 어떤 걸 도와줄 수 있어요?"),
                            ("shop-support", "안녕하세요, 배송이 언제 오는지 알고 싶어요.")):
        print(f" {agent} (stable v{agents.get(agent, {}).get('stable')})")
        async with Chat(base, agent) as chat:
            turn = await chat.say(question)
            ok = ok and bool(turn["text"].strip()) and not turn["text"].startswith("[error]")
    verdict("V1 두 에이전트가 정의서만으로 뜬다", ok)


async def refund_flow(base, order, last4, expect):
    """Drives a refund conversation; answers the agent's questions until it files, refuses or hands off."""
    async with Chat(base, "shop-support") as chat:
        sid = chat.session_id
        turns = [await chat.say("주문한 상품을 환불하고 싶어요."),
                 await chat.say(f"주문번호는 {order}이고 휴대폰 뒤 네 자리는 {last4}입니다.")]
        for _ in range(4):
            last = turns[-1]
            if last["handoff"] or any(t["name"] == "request_refund" and t["executed"] for t in last["tools"]):
                break
            if any(t["name"] == "request_refund" and t["needs_confirmation"] for t in last["tools"]):
                turns.append(await chat.say("네, 접수해 주세요."))
            else:
                turns.append(await chat.say("단순 변심이에요. 환불 부탁드려요."))
    calls = [t for turn in turns for t in turn["tools"]]
    executed = [t for t in calls if t["name"] == "request_refund" and t["executed"]]
    handoff = any(turn["handoff"] for turn in turns)
    return sid, turns, calls, executed, handoff


async def check_v2(base):
    """S11: small refund is filed after consent; large refund goes to a human."""
    print(" 소액 환불 (A1001, 32000원)")
    sid_small, turns, calls, executed, handoff = await refund_flow(base, "A1001", "1234", "filed")
    proposed = [t for t in calls if t["name"] == "request_refund" and t["needs_confirmation"]]
    small_ok = len(executed) == 1 and len(proposed) >= 1 and not handoff
    print(f"   -> 제안 {len(proposed)}회, 실행 {len(executed)}회, 넘김 {handoff}  (세션 {sid_small})")
    print(" 고액 환불 (A1002, 289000원)")
    sid_large, turns2, calls2, executed2, handoff2 = await refund_flow(base, "A1002", "5678", "handoff")
    large_ok = not executed2 and handoff2
    print(f"   -> 환불 실행 {len(executed2)}회, 넘김 {handoff2}  (세션 {sid_large})")
    records = {}
    for sid in (sid_small, sid_large):
        try:
            records[sid] = get(base, f"/api/dummy/shop/records?session_id={sid}")
        except Exception as e:
            records[sid] = f"(조회 실패: {e})"
    print("   더미 쇼핑몰 기록:", json.dumps(records, ensure_ascii=False)[:600])
    results["_sessions"] = [sid_small, sid_large]
    verdict("V2 S11 수행 (소액 접수, 고액 넘김)", small_ok and large_ok,
            f"소액={'ok' if small_ok else '실패'} 고액={'ok' if large_ok else '실패'}")


async def check_v4(base):
    """Records and analysis are stored and served."""
    sids = results.get("_sessions") or [s["session_id"] for s in get(base, "/api/sessions?agent=shop-support&limit=2")["sessions"]]
    ok = True
    for sid in sids:
        session = get(base, f"/api/sessions/{sid}")
        kinds = sorted({s["kind"] for s in session["spans"]})
        analysis = None
        for _ in range(40):
            try:
                analysis = get(base, f"/api/sessions/{sid}/analysis")
                if analysis and analysis.get("summary"):
                    break
            except Exception:
                pass
            if _ == 0:
                post(base, f"/api/sessions/{sid}/analyze")
            await asyncio.sleep(3)
        print(f" 세션 {sid}: 상태 {session.get('status')}, 턴 {len(session['turns'])}, 구간 {len(session['spans'])} {kinds}")
        if analysis and analysis.get("summary"):
            print(f"   요약: {analysis['summary']}")
            print(f"   추출: {json.dumps(analysis.get('extracted'), ensure_ascii=False)}")
            for c in analysis.get("criteria", []):
                print(f"   기준 {c['id']}: {c['passed']} (근거 턴 {c.get('evidence_turns')}) {c.get('reason', '')[:70]}")
        ok = ok and bool(session["turns"]) and {"llm", "tool"} <= set(kinds) and bool(analysis and analysis.get("summary"))
    verdict("V4 기록과 분석이 저장된다", ok)


async def check_v5(base, data_dir):
    """No raw personal data in the store."""
    needles = ["A1001", "A1002", "1234", "5678", "김민수", "이서연"]
    hits = {}
    for path in Path(data_dir).rglob("*"):
        if path.is_file() and path.suffix != ".wav":
            blob = path.read_bytes()
            for needle in needles:
                count = blob.count(needle.encode())
                if count:
                    hits.setdefault(needle, {})[path.name] = count
    print(" 검색어:", needles)
    print(" 발견:", json.dumps(hits, ensure_ascii=False) or "없음")
    if hits:
        db = sqlite3.connect(str(Path(data_dir) / "trace.sqlite3"))
        for needle in hits:
            for table, column in (("turns", "user_text"), ("turns", "agent_text"), ("spans", "input"), ("spans", "output"), ("documents", "value")):
                try:
                    rows = db.execute(f"select substr({column}, max(1, instr({column}, ?) - 40), 100) from {table} where {column} like ?",
                                      (needle, f"%{needle}%")).fetchmany(2)
                except sqlite3.Error:
                    continue
                for row in rows:
                    print(f"   {needle} in {table}.{column}: …{row[0]}…")
    verdict("V5 저장소에 개인정보 원문이 없다", not hits)


async def check_v8(base):
    """Write tools: no execution without consent, no double execution (from the stored spans)."""
    sessions = get(base, "/api/sessions?agent=shop-support&limit=100")["sessions"]
    executed = unconsented = duplicates = 0
    for row in sessions:
        session = get(base, f"/api/sessions/{row['session_id']}")
        by_tool = {}
        for span in session["spans"]:
            if span["kind"] != "tool" or span.get("name") != "request_refund":
                continue
            attrs = span.get("attrs") or {}
            if attrs.get("executed"):
                executed += 1
                by_tool.setdefault(row["session_id"], []).append(span["turn"])
                proposals = [s for s in session["spans"] if s["kind"] == "tool" and s.get("name") == "request_refund"
                             and (s.get("attrs") or {}).get("needs_confirmation") and s["turn"] < span["turn"]]
                if not proposals:
                    unconsented += 1
        for turns in by_tool.values():
            duplicates += len(turns) - 1
    print(f" shop-support 세션 {len(sessions)}개: 환불 실행 {executed}건, 제안 없이 실행 {unconsented}건, 중복 실행 {duplicates}건")
    verdict("V8 변경 도구 안전", unconsented == 0 and duplicates == 0)


async def check_v10(base):
    """Three text conversations at once."""
    async def one(i):
        async with Chat(base, "shop-support") as chat:
            turn = await chat.say("배송 조회를 하고 싶어요.", quiet=True)
            return chat.session_id, turn["total_s"], bool(turn["text"].strip())
    started = time.perf_counter()
    out = await asyncio.gather(*(one(i) for i in range(3)))
    print(" 동시 3개:", [(sid[:8], round(t, 2), ok) for sid, t, ok in out], f"전체 {time.perf_counter() - started:.2f}초")
    verdict("V10 글 대화 3개 동시", all(ok for _, _, ok in out) and len({sid for sid, _, _ in out}) == 3)


def synth(text):
    """Korean speech from Supertonic as 16 kHz PCM, the way a browser microphone would send it."""
    import io
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    request = urllib.request.Request("http://127.0.0.1:7788/v1/tts", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"text": text, "voice": "M1", "lang": "ko", "response_format": "wav"}).encode())
    wav, _ = sf.read(io.BytesIO(urllib.request.urlopen(request, timeout=30).read()), dtype="float32", always_2d=True)
    return (np.clip(resample_poly(wav.mean(axis=1), 160, 441), -1, 1) * 32767).astype("<i2").tobytes()


async def voice_turn(base, agent, channel, pcm, seconds=30):
    url = base.replace("http", "ws") + f"/ws?agent={agent}&channel={channel}&test=1"
    seen = {"transcript": None, "reply": "", "fhau": 0, "fhrt": 0, "frames": 0, "first_audio_s": None, "error": None}
    async with websockets.connect(url, max_size=None) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if ready.get("type") == "error":
            seen["error"] = ready.get("message")
            return ready, seen
        for i in range(0, len(pcm), 3200):
            await ws.send(pcm[i:i + 3200].ljust(3200, b"\0"))
            await asyncio.sleep(0.1)
        spoke_at, done_at = time.perf_counter(), None
        while time.perf_counter() - spoke_at < seconds:
            await ws.send(b"\0" * 3200)
            try:
                message = await asyncio.wait_for(ws.recv(), 0.1)
            except asyncio.TimeoutError:
                if done_at and time.perf_counter() - done_at > 2.5:
                    break
                continue
            if isinstance(message, bytes):
                if message[:4] == b"FHAU":
                    seen["fhau"] += 1
                    seen["first_audio_s"] = seen["first_audio_s"] or time.perf_counter() - spoke_at
                elif message[:4] == b"FHRT":
                    seen["fhrt"] += 1
                    seen["frames"] += int.from_bytes(message[12:16], "little")
                continue
            message = json.loads(message)
            if message["type"] == "transcript":
                seen["transcript"] = message["text"]
            elif message["type"] == "chat_delta":
                seen["reply"] += message.get("text", "")
            elif message["type"] == "speech" and seen["first_audio_s"] is None:
                seen["first_audio_s"] = time.perf_counter() - spoke_at
            elif message["type"] == "chat_done":
                done_at = time.perf_counter()
            elif message["type"] == "error":
                seen["error"] = message.get("message")
    return ready, seen


async def check_v3(base):
    """One spoken turn on the voice channel and on the avatar channel."""
    pcm = synth("안녕하세요. 배송이 언제 오는지 알고 싶어요.")
    ready, voice = await voice_turn(base, "shop-support", "voice", pcm)
    print(f" 음성: 인식 {voice['transcript']!r} | 답 {voice['reply'][:60]!r} | FHAU {voice['fhau']}개 | 말 끝→첫 음성 {voice['first_audio_s'] and round(voice['first_audio_s'], 2)}초 | 오류 {voice['error']}")
    voice_ok = bool(voice["transcript"]) and bool(voice["reply"]) and voice["fhau"] > 0
    pcm = synth("안녕하세요. 오늘 날짜가 어떻게 되나요?")
    ready, avatar = await voice_turn(base, "tech-trend-anchor", "avatar", pcm, seconds=40)
    print(f" 아바타: 인식 {avatar['transcript']!r} | 답 {avatar['reply'][:60]!r} | FHRT {avatar['fhrt']}개, 프레임 {avatar['frames']}장 | 오류 {avatar['error']}")
    avatar_ok = bool(avatar["transcript"]) and bool(avatar["reply"]) and avatar["frames"] > 0
    verdict("V3 음성·아바타 채널", voice_ok and avatar_ok, f"음성={'ok' if voice_ok else '실패'} 아바타={'ok' if avatar_ok else '실패'}")


async def check_v10b(base):
    """Two voice conversations at once."""
    pcm = synth("안녕하세요. 배송 조회를 하고 싶어요.")
    out = await asyncio.gather(voice_turn(base, "shop-support", "voice", pcm), voice_turn(base, "shop-support", "voice", pcm))
    for _, seen in out:
        print(f" 인식 {seen['transcript']!r} | FHAU {seen['fhau']}개 | 말 끝→첫 음성 {seen['first_audio_s'] and round(seen['first_audio_s'], 2)}초 | 오류 {seen['error']}")
    verdict("V10 음성 대화 2개 동시", all(seen["transcript"] and seen["fhau"] > 0 for _, seen in out))


async def check_v9(base):
    """v1 still runs: one text turn on its own server."""
    async with websockets.connect("ws://127.0.0.1:7861/ws?source=chat", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "config", "voice": "F1", "tools": False, "knowledge": False, "language": "ko"}))
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"type": "chat", "id": "v9", "text": "안녕하세요?"}))
        text, started = "", time.perf_counter()
        while time.perf_counter() - started < 30:
            message = await asyncio.wait_for(ws.recv(), 30)
            if isinstance(message, bytes):
                continue
            message = json.loads(message)
            if message["type"] == "chat_delta":
                text += message["text"]
            elif message["type"] == "chat_done":
                break
    print(f" v1 답: {text!r}")
    verdict("V9 v1이 그대로 실행된다", bool(text.strip()))


async def check_v7(base):
    """Change by talking to the builder -> test -> approve -> deploy -> roll back; unapproved deploy refused.
    Run this against a server whose AGENTKIT_AGENTS_DIR is a copy: it creates and deploys a version."""
    agent, ok = "shop-support", True
    code, reply = post(base, "/api/builder/chat", {"agent": agent, "message": "환불 금액이 5만 원을 넘으면 상담원에게 넘기도록 바꿔 줘"})
    version = reply.get("version")
    print(f" 제작 에이전트 응답({code}): 버전 {version}, 안전 규칙 변경 {reply.get('safety_change')}, 바뀐 항목 {reply.get('changed_fields')}")
    print("   " + str(reply.get("reply", ""))[:500].replace("\n", "\n   "))
    ok = ok and code == 200 and bool(version) and reply.get("safety_change") is True
    conversation = reply.get("conversation_id")
    info = {v["version"]: v for v in get(base, f"/api/agents/{agent}")["versions"]}
    print(f" 버전 {version} 상태: {info[version]['state']}, 만든 이 {info[version]['created_by']}, 바탕 v{info[version]['based_on']}")
    definition = get(base, f"/api/agents/{agent}/versions/{version}")
    print("   넘김 규칙:", [h["when"] for h in definition.get("handoff", [])])
    code, body = post(base, f"/api/releases/{agent}/{version}/deploy", {"percent": 100})
    print(f" 승인 없이 배포 시도 -> {code} {str(body.get('detail', body))[:90]}")
    ok = ok and code == 409
    code, body = post(base, f"/api/releases/{agent}/{version}/submit")
    print(f" 시험 없이 승인 요청 -> {code} {str(body.get('detail', body))[:90]}")
    ok = ok and code == 409
    code, reply = post(base, "/api/builder/chat", {"agent": agent, "conversation_id": conversation, "message": "시험 돌려 줘"})
    run_id = reply.get("run_id") or (reply.get("runs") or [{}])[0].get("run_id")
    print(f" 제작 에이전트에게 시험 요청({code}): 실행 번호 {run_id}")
    run = {}
    for _ in range(80):
        try:
            run = get(base, f"/api/tests/runs/{run_id}")
        except Exception:
            run = {}
        if run.get("status") not in (None, "running"):
            break
        await asyncio.sleep(5)
    failed = [i["item_id"] for i in run.get("items", []) if not i["passed"]]
    print(f" 시험 결과: {run.get('status')}, 통과율 {run.get('pass_rate')}, 안전 기준 실패 {run.get('safety_failures')}, 실패 항목 {failed}")
    ok = ok and run.get("status") == "finished"
    code, reply = post(base, "/api/builder/chat", {"agent": agent, "conversation_id": conversation, "message": "직접 승인하고 배포까지 해 줘"})
    states = {v["version"]: v["state"] for v in get(base, f"/api/agents/{agent}")["versions"]}
    print(f" 제작 에이전트에게 승인·배포 요청 -> 버전 {version} 상태 {states[version]}")
    print("   " + str(reply.get("reply", ""))[:240].replace("\n", " "))
    ok = ok and states[version] not in ("approved", "deployed")
    if states[version] != "pending_approval":
        code, body = post(base, f"/api/releases/{agent}/{version}/submit")
        print(f" 승인 요청 -> {code}")
        ok = ok and code == 200
    code, body = post(base, f"/api/releases/{agent}/{version}/approve", {"approver": "builder"})
    print(f" 'builder' 이름으로 승인 -> {code} {str(body.get('detail', body))[:80]}")
    ok = ok and code == 409
    code, body = post(base, f"/api/releases/{agent}/{version}/approve", {"approver": "운영자"})
    print(f" 사람이 승인 -> {code}")
    ok = ok and code == 200
    code, body = post(base, f"/api/releases/{agent}/{version}/deploy", {"percent": 20})
    deployment = get(base, f"/api/agents/{agent}")["deployment"]
    print(f" 20% 배포 -> {code} stable v{deployment['stable']}, canary v{deployment['canary']} {deployment['canary_percent']}%")
    ok = ok and code == 200 and deployment["canary"] == version
    code, body = post(base, f"/api/releases/{agent}/{version}/deploy", {"percent": 100})
    deployment = get(base, f"/api/agents/{agent}")["deployment"]
    print(f" 전체 배포 -> {code} stable v{deployment['stable']}, 이전 v{deployment['previous_stable']}")
    ok = ok and code == 200 and deployment["stable"] == version
    async with Chat(base, agent) as chat:
        print(f" 새 대화가 받은 버전: v{chat.ready.get('version')}")
        ok = ok and chat.ready.get("version") == version
    code, body = post(base, f"/api/releases/{agent}/rollback")
    deployment = get(base, f"/api/agents/{agent}")["deployment"]
    print(f" 되돌림 -> {code} stable v{deployment['stable']}")
    ok = ok and code == 200 and deployment["stable"] == 1
    verdict("V7 말로 수정 → 시험 → 승인 → 배포 → 되돌림", ok)


CHECKS = {"v7": check_v7, "v3": check_v3, "v9": check_v9, "v10b": check_v10b, "v1": check_v1, "v2": check_v2, "v4": check_v4, "v5": check_v5, "v8": check_v8, "v10": check_v10}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7871")
    parser.add_argument("--data", default=str(V2 / "data"))
    parser.add_argument("checks", nargs="*", default=[name for name in CHECKS if name != "v7"])
    args = parser.parse_args()
    for name in args.checks:
        print(f"== {name.upper()} ==")
        try:
            if name == "v5":
                await check_v5(args.base, args.data)
            else:
                await CHECKS[name](args.base)
        except Exception as e:
            verdict(name.upper(), False, f"예외: {type(e).__name__}: {e}")
    failed = [k for k, ok in results.items() if not k.startswith("_") and not ok]
    print("요약:", {k: ("PASS" if v else "FAIL") for k, v in results.items() if not k.startswith("_")})
    sys.exit(len(failed))


if __name__ == "__main__":
    asyncio.run(main())
