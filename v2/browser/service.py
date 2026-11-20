"""The browsing "brain" as a service: the voice agent hands it a task in words and gets words back.

    /data/miniforge3/bin/python3 service.py            # port 7895; also starts the dummy office on 7890

Runs in its own process and interpreter (it needs Playwright; the voice server does not), and is
reached through three tool contracts of kind "http" (v2/tools/web_task*.yaml):

    POST /task     {arguments: {task}}       start a task for this session; returns when it is done,
                                             needs an answer from the user, or is about to press an
                                             irreversible button
    POST /answer   {arguments: {answer}}     the user's answer to the question the task asked
    POST /confirm  {arguments: {task_id}}    press the button the task stopped at (the gateway calls this
                                             only after the user consented to the sentence from /confirm_preview)

Every reply uses the adapters' wire format {"ok", "text", "data", "error_type", "executed"}.
One task per session at a time; a task that waits for the user is dropped after IDLE seconds or when
the session starts another task. Secrets stay in VAULT, inside this process.
"""
import argparse, itertools, json, os, queue, threading, time, urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

import agent

IDLE = 300          # seconds a paused task waits for the user before it gives up
# Seconds one tool call waits for the task. After that the call answers "still working", the
# conversation goes on, and the task's next stop is reported to the voice server as a notice.
# 6 s lets a plain lookup (about 5 s) answer in the same reply; with 4 s the agent said "I will let you know"
# and then the answer one second later.
WAIT = float(os.environ.get("BROWSER_WAIT", 6))
def _service_header():
    """The voice server asks programs on this machine for its service token once an owner login is set
    (agentkit/auth.py). The token file is read each time, so it may be created after this service started."""
    path = os.path.join(os.environ.get("AGENTKIT_SECRETS_DIR", os.path.expanduser("~/.agentkit")), "service.token")
    try:
        with open(path) as f:
            return {"X-Agentkit-Service": f.read().strip()}
    except OSError:
        return {}


AGENT_URLS = os.environ.get("AGENT_URLS", "http://127.0.0.1:7871,http://127.0.0.1:7872").split(",")
MODEL = os.environ.get("BROWSER_MODEL", "gemma4:26b")
SITE = os.environ.get("OFFICE_URL", "http://127.0.0.1:7890")
VAULT = {SITE: {"username": "assistant", "password": "blue-heron-42"}}      # demo vault: never logged, never sent to the model
_ids = itertools.count(1)
JOBS, LOCK = {}, threading.Lock()


class Job:
    """One task in its own thread (Playwright's sync API has to stay on the thread that started it)."""

    def __init__(self, session_id, task):
        self.session_id, self.task = session_id, task
        self.inbox, self.changed = queue.Queue(), threading.Event()
        self.state, self.text, self.gate_id, self.gate = "running", "", None, None
        self.cancelled, self.result, self.started = False, None, time.time()
        self.guard, self.background = threading.Lock(), False
        threading.Thread(target=self.run, daemon=True).start()

    def stop_at(self, state, text="", gate=None):
        with self.guard:
            self.state, self.text, self.gate = state, text, gate
            self.gate_id = f"g{next(_ids)}" if state == "confirm" else None
            self.changed.set()
            tell = self.background and not self.cancelled
            self.background = False
        if tell:
            # nobody is waiting on an HTTP call any more: bring the news to the conversation
            self.changed.clear()
            threading.Thread(target=notify, args=(self.session_id, self.describe()["text"]), daemon=True).start()

    def wait_user(self):
        """Blocks the task until the user's word arrives. None: gave up waiting, or cancelled."""
        try:
            value = self.inbox.get(timeout=IDLE)
        except queue.Empty:
            value = None
        self.state = "running"
        return None if self.cancelled else value

    def run(self):
        def confirm(text):
            self.stop_at("confirm", text, gate=text)
            return bool(self.wait_user())

        def answer(question):
            self.stop_at("asked", question)
            return self.wait_user()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_context(locale="ko-KR").new_page()
                self.result = agent.run_task(page, self.task, MODEL, VAULT, [SITE], SITE + "/", confirm=confirm, answer_question=answer,
                                             context=f"(오늘은 {date.today().isoformat()}입니다.)")
                browser.close()
            self.stop_at(self.result["status"], self.result.get("answer") or "")
        except Exception as e:
            self.stop_at("error", f"{type(e).__name__}: {str(e)[:200]}")

    def next_stop(self):
        """Waits up to WAIT seconds for the task to reach done / asked / confirm / ...; if it has not,
        answers "still working" and leaves the rest to a notice."""
        self.changed.wait(WAIT)
        with self.guard:
            if not self.changed.is_set():
                self.background = True
                return reply(True, "웹 작업을 시작했고 아직 진행 중입니다. 결과는 끝나는 대로 시스템 알림으로 옵니다. "
                                   "지금은 사용자에게 맡아서 처리하고 있고 끝나면 알려 주겠다고만 짧게 말하세요. "
                                   "결과를 짐작해서 말하면 안 되고, 같은 일을 다시 맡기지도 마세요. 그동안 사용자의 다른 말에는 평소처럼 답하면 됩니다.",
                             {"status": "running"})
            self.changed.clear()
        return self.describe()

    def describe(self):
        """The stop the task is at, in words for the voice agent."""
        seconds = round(time.time() - self.started, 1)
        steps = len((self.result or {}).get("steps", []))
        if self.state == "done":
            return reply(True, f"웹 작업 결과: {self.text}", {"status": "done", "seconds": seconds, "steps": steps})
        if self.state == "asked":
            return reply(True, f"웹 작업을 계속하려면 사용자의 답이 필요합니다. 사용자에게 이렇게 물어보세요: \"{self.text}\" "
                               "사용자의 답을 들은 뒤 web_task_answer 도구에 그 답을 넘기세요. 스스로 정해서 답하면 안 됩니다.",
                         {"status": "asked", "question": self.text})
        if self.state == "confirm":
            return reply(True, f"아직 실행하지 않았습니다. 웹 화면에서 되돌릴 수 없는 단추를 누르기 직전에 멈췄습니다 ({self.text}). "
                               f"사용자가 맡긴 일이 맞으면 지금 web_task_confirm 도구를 task_id \"{self.gate_id}\"로 호출하세요. "
                               "그 도구가 사용자에게 읽어 줄 확인 문장을 돌려줍니다. 등록했다, 보냈다고 말하면 안 됩니다.",
                         {"status": "confirm", "task_id": self.gate_id, "summary": summary_of(self.text)})
        if self.state == "declined":
            return reply(True, "사용자가 허락하지 않아 실행하지 않았습니다. 아무것도 바뀌지 않았습니다.", {"status": "declined"})
        return reply(False, f"웹 작업을 끝내지 못했습니다 ({self.state}). {self.text}".strip(), error_type="system_error")


def notify(session_id, text):
    """Tells the voice server that holds this conversation. A conversation that has ended is not an error."""
    body = json.dumps({"text": text}).encode()
    for base in AGENT_URLS:
        try:
            request = urllib.request.Request(f"{base.strip()}/api/sessions/{session_id}/notify", data=body,
                                             headers={"Content-Type": "application/json", **_service_header()})
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:
            continue
    return False


def summary_of(gate_text):
    """ "'일정 등록' 단추를 누르려 합니다. 입력 내용: 제목=예산 회의, ..." -> words for the confirmation sentence."""
    button = gate_text.split("'")[1] if "'" in gate_text else "실행"
    form = gate_text.split("입력 내용:", 1)[1].strip() if "입력 내용:" in gate_text else ""
    form = ", ".join(part.replace("=", " ") for part in form.split(", ") if part and not part.endswith("="))
    return f"{form} 내용으로 {button}을" if form else f"{button}을"


def reply(ok, text, data=None, error_type=None, executed=None):
    out = {"ok": ok, "text": text, "data": data, "error_type": error_type}
    if executed is not None:
        out["executed"] = executed
    return out


def current(session_id):
    with LOCK:
        return JOBS.get(session_id)


def handle(path, arguments, context):
    session_id = str(context.get("session_id") or "default")
    job = current(session_id)
    if path == "/task":
        task = str(arguments.get("task") or "").strip()
        if not task:
            return reply(False, "맡길 일이 비어 있습니다.", error_type="invalid_input")
        if job is not None and job.state in ("asked", "confirm", "running"):
            job.cancelled = True; job.inbox.put(None)          # a new task replaces one that was left waiting
        job = Job(session_id, task)
        with LOCK:
            JOBS[session_id] = job
        return job.next_stop()
    if job is None:
        return reply(False, "진행 중인 웹 작업이 없습니다. web_task로 먼저 일을 맡기세요.", error_type="precondition_failed")
    if path == "/answer":
        if job.state != "asked":
            return reply(False, "지금 웹 작업은 사용자의 답을 기다리고 있지 않습니다.", error_type="precondition_failed")
        job.changed.clear(); job.inbox.put(str(arguments.get("answer") or ""))
        return job.next_stop()
    if path in ("/confirm_preview", "/confirm"):
        if job.state != "confirm" or str(arguments.get("task_id") or "") != job.gate_id:
            return reply(False, "확인을 기다리는 웹 작업이 없거나 task_id가 다릅니다. 아무것도 실행하지 않았습니다.", error_type="precondition_failed")
        if path == "/confirm_preview":
            return reply(True, "", {"summary": summary_of(job.gate)})
        job.changed.clear(); job.inbox.put(True)
        out = job.next_stop()
        out["executed"] = True
        return out
    return reply(False, "없는 경로입니다.", error_type="not_found")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            out = handle(self.path, body.get("arguments") or {}, body.get("context") or {})
        except Exception as e:
            out = reply(False, f"웹 작업 서비스 오류: {type(e).__name__}", error_type="system_error")
        data = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        data = json.dumps({"ok": True, "model": MODEL, "site": SITE, "jobs": {k: j.state for k, j in JOBS.items()}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7895)
    ap.add_argument("--office-port", type=int, default=7890, help="0: do not start the dummy office here")
    a = ap.parse_args()
    if a.office_port:
        import office_site
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", a.office_port), office_site.Handler).serve_forever, daemon=True).start()
    print(f"browser service on 127.0.0.1:{a.port}, model {MODEL}, site {SITE}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
