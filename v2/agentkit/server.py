"""The v2 server: WebSocket /ws (text channel here, voice and avatar handed to runtime.voice),
the REST routers of the other modules, and the web pages.

    cd v2 && python -m agentkit.server --port 7871

Every other module is optional: its router is mounted if the module can be imported.
"""
import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import config

logger = logging.getLogger("agentkit.server")

ROUTER_MODULES = ("agentkit.definition.api", "agentkit.tools.api", "agentkit.trace.api",
                  "agentkit.eval.api", "agentkit.builder.api", "agentkit.google_connect")
PAGES = {"/": "index.html", "/monitor": "monitor.html", "/studio": "studio.html", "/connect": "connect.html"}
CHANNELS = ("text", "voice", "avatar")
OPENING_ID = "opening"   # the message id of the agent's opening words and screens


def _flag(value):
    return str(value or "").lower() in ("1", "true", "yes", "on")


ANALYSIS_WAIT_S = 300   # how long an analysis waits for live conversations to end before it runs anyway


async def _analyze(session_id, app=None):
    """Runs the post-conversation analysis, if the evaluation module is there. Never raises.

    The analysis makes a dozen LLM calls on the server the live conversations use, and was seen to
    triple their reply delay. So analyses run one at a time, and only while nobody is talking."""
    try:
        from agentkit.eval import analyze_session
    except ImportError:
        return
    loop = asyncio.get_running_loop()
    if app is not None:
        await app.state.analysis_lock.acquire()
    try:
        if app is not None:
            deadline = loop.time() + ANALYSIS_WAIT_S
            while app.state.sessions and loop.time() < deadline:
                await asyncio.sleep(2)
        try:
            from agentkit.trace import get_recorder
            flush = getattr(get_recorder(), "flush", None)
            if flush is not None:
                await loop.run_in_executor(None, flush)   # the analysis reads what was stored
        except ImportError:
            pass
        if inspect.iscoroutinefunction(analyze_session):
            await analyze_session(session_id)
        else:
            result = await loop.run_in_executor(None, analyze_session, session_id)
            if inspect.isawaitable(result):
                await result
        logger.info(f"[{session_id}] analysis done")
    except Exception as e:
        logger.warning(f"[{session_id}] analysis failed: {e!r}")
    finally:
        if app is not None:
            app.state.analysis_lock.release()


def create_app(engine_factory=None):
    """engine_factory: async (agent_id, version=, channel=, test=, settings=) -> ReplyEngine.
    Default: agentkit.runtime.create_engine. Tests pass their own."""
    @contextlib.asynccontextmanager
    async def lifespan(app):
        sweeper = asyncio.create_task(app.state.idle_sweeper())     # unloads engines nobody uses (see "engines")
        try:
            yield
        finally:
            sweeper.cancel()

    app = FastAPI(title="agentkit v2", lifespan=lifespan)
    from agentkit import auth
    app.add_middleware(auth.OwnerOnly)   # owner login, once a password is set (agentkit/auth.py)
    auth.add_routes(app)
    app.state.mounted = []
    app.state.missing = {}
    app.state.sessions = {}          # session_id -> {"agent", "channel", "started_at"}; live conversations
    app.state.notifiers = {}         # session_id -> callable(text): hands news to that live conversation
    app.state.background = set()     # analysis tasks, kept so they are not collected mid-run
    app.state.analysis_lock = asyncio.Lock()

    for name in ROUTER_MODULES:
        try:
            module = importlib.import_module(name)
            app.include_router(module.router)
            app.state.mounted.append(name)
        except ImportError as e:
            app.state.missing[name] = str(e)
        except Exception as e:   # a half-built module must not keep the server down
            logger.warning(f"Router {name} not mounted: {e!r}")
            app.state.missing[name] = repr(e)
    logger.info(f"Routers mounted: {app.state.mounted or 'none'}; missing: {list(app.state.missing) or 'none'}")

    async def make_engine(agent_id, **options):
        if engine_factory is not None:
            return await engine_factory(agent_id, **options)
        from agentkit.runtime import create_engine
        return await create_engine(agent_id, **options)

    # ------------------------------------------------------------------------------ engines
    # The video engine (about 45 s to load) and the speech recogniser (a few seconds) are not loaded
    # at start-up. They can be prepared ahead of a conversation, dropped by hand, and are dropped
    # on their own after config.ENGINE_IDLE_S without use.

    def engine_parts():
        """(video holder, shared audio), each None if its module cannot be imported here."""
        try:
            from agentkit.runtime import avatar
            video = avatar.holder()
        except Exception:
            video = None
        try:
            from agentkit.runtime import audio
            sound = audio.shared()
        except Exception:
            sound = None
        return video, sound

    def listening_sessions():
        return [s for s in app.state.sessions.values() if s.get("channel") in ("voice", "avatar")]

    def engines_status():
        video, sound = engine_parts()
        limit = config.ENGINE_IDLE_S

        def with_countdown(status):
            if status and status.get("state") == "ready" and status.get("idle_s") is not None and limit > 0:
                status["unloads_in_s"] = max(0, round(limit - status["idle_s"]))
            return status
        stt = with_countdown(sound.whisper_status()) if sound is not None else None
        if stt and stt.get("state") == "ready" and listening_sessions():
            stt.pop("unloads_in_s", None)
            stt["state"] = "in_use"
        return {"video": with_countdown(video.status()) if video is not None else None, "stt": stt,
                "idle_unload_s": limit}

    @app.get("/api/engines")
    async def get_engines():
        return engines_status()

    @app.post("/api/engines/warmup")
    async def warm_engines(body: dict = None):
        """{"video": bool, "stt": bool, "image": path} (default: both). Starts loading and returns at once;
        poll GET /api/engines. Safe to call again, and a conversation started meanwhile waits for the same load."""
        body = body or {}
        video, sound = engine_parts()
        if body.get("stt", True) and sound is not None:
            sound.warm_whisper()
        if body.get("video", True) and video is not None:
            try:
                video.warm(body.get("image"))
            except Exception as e:
                return JSONResponse({**engines_status(), "error": str(e)}, status_code=400)
        return engines_status()

    @app.post("/api/engines/unload")
    async def unload_engines(body: dict = None):
        """{"video": bool, "stt": bool} (default: both). An engine a conversation is using stays."""
        body = body or {}
        video, sound = engine_parts()
        done = {}
        if body.get("video", True) and video is not None:
            done["video"] = video.unload()
        if body.get("stt", True) and sound is not None:
            done["stt"] = False if listening_sessions() else sound.unload_whisper()
        return {**engines_status(), "unloaded": done}

    async def idle_sweeper():
        while True:
            await asyncio.sleep(30)
            try:
                limit = config.ENGINE_IDLE_S
                video, sound = engine_parts()
                if video is not None and video.unload_if_idle(limit):
                    logger.info("Video engine unloaded after %.0f s without use.", limit)
                if sound is not None and limit > 0 and not listening_sessions():
                    status = sound.whisper_status()
                    if status.get("state") == "ready" and (status.get("idle_s") or 0) >= limit and sound.unload_whisper():
                        logger.info("Speech recognition unloaded after %.0f s without use.", limit)
            except Exception as e:
                logger.warning("idle sweep failed: %r", e)

    app.state.idle_sweeper = idle_sweeper       # started by the lifespan below

    @app.post("/api/sessions/{session_id}/notify")
    async def notify_session(session_id: str, body: dict):
        """News for a live conversation from a system the agent handed work to (v2/browser/service.py):
        the agent tells the customer at the next quiet moment. 404 if the conversation is over."""
        notifier = app.state.notifiers.get(session_id)
        text = str((body or {}).get("text") or "").strip()
        if notifier is None:
            return JSONResponse({"ok": False, "error": "no live session"}, status_code=404)
        if not text:
            return JSONResponse({"ok": False, "error": "text is empty"}, status_code=400)
        notifier(text)
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "routers": app.state.mounted, "missing": app.state.missing,
                "sessions": len(app.state.sessions),
                # an internal test deployment: the conversation page starts with recording on
                "internal_testing": _flag(os.environ.get("AGENTKIT_INTERNAL_TESTING", "1"))}

    # ------------------------------------------------------------------------------ pages

    def page(filename):
        async def serve():
            path = config.WEB_DIR / filename
            if path.is_file():
                return FileResponse(path)
            return JSONResponse({"error": f"web/{filename} 파일이 없습니다."}, status_code=404)
        return serve

    for route, filename in PAGES.items():
        app.add_api_route(route, page(filename), methods=["GET"], include_in_schema=False)

    @app.get("/web/{path:path}", include_in_schema=False)
    async def web_file(path: str):
        root = config.WEB_DIR.resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(target)

    # ------------------------------------------------------------------------------ conversations

    def end_session(engine):
        """After the engine is closed: forget the session and analyse it in the background."""
        app.state.sessions.pop(engine.session_id, None)
        if getattr(engine, "turn", 0) > 0:
            task = asyncio.create_task(_analyze(engine.session_id, app))
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        query = ws.query_params
        agent_id = query.get("agent") or ""
        channel = query.get("channel") or "text"
        test = _flag(query.get("test"))
        settings = {key: query[key] for key in ("language", "reply_length") if query.get(key)}
        if query.get("length"):
            settings["reply_length"] = query["length"]

        async def refuse(message):
            try:
                await ws.send_json({"type": "error", "message": message})
                await ws.close()
            except Exception:
                pass

        if not agent_id:
            return await refuse("agent 값이 필요합니다. 예: /ws?agent=shop-support&channel=text")
        if channel not in CHANNELS:
            return await refuse(f"알 수 없는 채널입니다: {channel}")
        try:
            version = int(query["version"]) if query.get("version") else None
        except ValueError:
            return await refuse(f"version 값이 숫자가 아닙니다: {query.get('version')}")
        try:
            engine = await make_engine(agent_id, version=version, channel=channel, test=test, settings=settings)
        except Exception as e:
            logger.warning(f"Engine for {agent_id!r} not created: {e!r}")
            return await refuse(f"에이전트를 시작하지 못했습니다: {e}")
        app.state.sessions[engine.session_id] = {"agent": agent_id, "channel": channel, "started_at": time.time()}
        notifiers = app.state.notifiers
        logger.info(f"[{engine.session_id}] {channel} session, agent {agent_id} v{engine.definition.version}")
        status = "completed"
        try:
            if channel == "text":
                notices = asyncio.Queue()
                notifiers[engine.session_id] = notices.put_nowait
                await serve_text(ws, engine, notices)
            else:
                try:
                    from agentkit.runtime import voice
                except ImportError:
                    await refuse("음성 채널이 설치되지 않았습니다.")
                    status = "abandoned"
                    return
                # the voice module sends its own "ready" and owns the socket until it disconnects
                parameters = inspect.signature(voice.serve_voice).parameters
                if "on_session" in parameters:
                    await voice.serve_voice(ws, engine, channel, dict(query), on_session=lambda session: notifiers.__setitem__(
                        engine.session_id, session.notify))
                elif "settings" in parameters or len(parameters) >= 4:
                    await voice.serve_voice(ws, engine, channel, dict(query))
                else:
                    await voice.serve_voice(ws, engine, channel)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception(f"[{engine.session_id}] session failed: {e!r}")
            status = "error"
            await refuse(f"대화가 오류로 끝났습니다: {e}")
        finally:
            if status == "completed" and getattr(engine, "turn", 0) == 0:
                status = "abandoned"   # connected and left without saying anything
            app.state.notifiers.pop(engine.session_id, None)
            try:
                await engine.close(status)
            except Exception as e:
                logger.warning(f"[{engine.session_id}] close failed: {e!r}")
            end_session(engine)

    return app


async def serve_text(ws, engine, notices=None):
    """The text channel: {"type":"chat","id","text"} in, the reply's events out (ARCHITECTURE 6).

    notices: an asyncio.Queue of news from outside (POST /api/sessions/{id}/notify); each becomes a
    turn in which the agent speaks first (engine.notify), once no reply is in progress."""
    definition = engine.definition
    send_lock = asyncio.Lock()

    async def send(payload):
        async with send_lock:
            await ws.send_json(payload)

    from agentkit.runtime import client_features
    features = client_features(definition)
    await send({"type": "ready", "session_id": engine.session_id, "agent": definition.id,
                "name": definition.name, "version": definition.version, "channel": "text",
                "language": getattr(engine, "language", None) or definition.language,
                "tools": list(definition.tools), **features})
    task = None

    async def answer(request_id, events):
        """One reply (or the opening, or the reaction to a touch), event by event. A newer message
        or "clear" cancels it."""
        first = True
        try:
            async for event in events:
                kind = event.type
                if kind == "delta":
                    await send({"type": "chat_delta", "id": request_id, "text": event.text})
                    if first:
                        first = False
                        engine.mark_output("reply", time.time())
                elif kind == "replace":
                    await send({"type": "chat_replace", "id": request_id, "text": event.text, **event.data})
                elif kind == "filler":
                    await send({"type": "filler", "id": request_id, "text": event.text})
                    engine.mark_output("filler", time.time())
                elif kind in ("tool", "knowledge", "llm"):
                    await send({**event.data, "type": kind, "id": request_id})
                elif kind == "display":
                    await send({"type": "display", "id": request_id, "kind": event.data.get("kind"),
                                "data": event.data.get("data"), "source": event.data.get("source", "")})
                elif kind == "piece" and request_id == OPENING_ID:
                    # the opening words are not streamed: they arrive whole, as a piece
                    await send({"type": "chat_delta", "id": request_id, "text": event.text})
                    engine.mark_output("reply", time.time())
                elif kind == "handoff":
                    await send({"type": "handoff", "id": request_id, "rule_id": event.data.get("rule_id", ""),
                                "reason": event.data.get("reason", "")})
                elif kind == "done":
                    await send({"type": "chat_done", "id": request_id, "text": event.text, **event.data})
                # "piece" is for speech; the text channel has shown it as deltas already
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception(f"[{engine.session_id}] reply failed: {e!r}")
            try:
                await send({"type": "error", "id": request_id, "message": f"답변을 만들지 못했습니다: {e}"})
                await send({"type": "chat_done", "id": request_id, "text": ""})
            except Exception:
                pass

    async def stop():
        nonlocal task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        task = None

    async def notice_pump():
        nonlocal task
        count = 0
        while True:
            text = await notices.get()
            while task is not None and not task.done():
                await asyncio.sleep(0.1)       # never over a reply in progress
            count += 1
            task = asyncio.create_task(answer(f"notice-{count}", engine.notify(text)))

    pump = asyncio.create_task(notice_pump()) if notices is not None and hasattr(engine, "notify") else None

    if features["opening"] and hasattr(engine, "open"):
        # The agent speaks first. Not a task: the opening calls are short lookups, and a message that
        # arrives meanwhile must not cancel them (the menu on screen comes from them).
        await answer(OPENING_ID, engine.open())

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("text")
            if raw is None:
                continue   # binary is audio; this is the text channel
            try:
                request = json.loads(raw)
            except ValueError:
                await send({"type": "error", "message": "JSON이 아닌 메시지입니다."})
                continue
            if not isinstance(request, dict):
                continue
            kind = request.get("type")
            if kind == "chat":
                text = str(request.get("text") or "").strip()
                if not text:
                    continue
                await stop()   # a new message interrupts the reply in progress
                task = asyncio.create_task(answer(request.get("id"), engine.reply(text, t_user_end=time.time())))
            elif kind == "action":
                # a touch on the screen: a customer turn like a typed one (docs/DISPLAY.md)
                name, arguments = str(request.get("name") or ""), request.get("arguments")
                await stop()
                if not hasattr(engine, "act"):
                    await send({"type": "chat_done", "id": request.get("id"), "text": "",
                                "error": "이 에이전트는 화면 동작을 받지 않습니다."})
                    continue
                task = asyncio.create_task(answer(
                    request.get("id"), engine.act(name, arguments if isinstance(arguments, dict) else {})))
            elif kind == "clear":
                await stop()
            elif kind == "config":
                options = getattr(engine, "set_options", None)
                if options is not None:
                    options(language=request.get("language"),
                            reply_length=request.get("reply_length") or request.get("length"))
            # "played" belongs to the voice channel
    finally:
        if pump is not None:
            pump.cancel()
        await stop()


def main():
    parser = argparse.ArgumentParser(description="agentkit v2 server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    parser.add_argument("--log_level", default="info")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
