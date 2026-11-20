"""
Shared helpers of the screen tests (WP7), and the runner for machines where the system python has
playwright but no pytest:

    cd v2 && python3 tests/test_web_support.py            # runs every test_web_*.py
    cd v2 && python3 -m pytest tests/test_web_*.py -q     # the same, if pytest is installed

The tests drive v2/web in headless Chromium against tests/mock_api.py on port 7884. The mock is started
here if nothing listens on the port, and stopped again at exit. Screenshots go to tests/screenshots/.
"""
from __future__ import annotations

import atexit
import contextlib
import importlib
import json
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 7884
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = HERE / "screenshots"
MOCK_PYTHON = "/data/miniforge3/envs/flashhead/bin/python"
ROUTE_MISSING = {"status": 404, "content_type": "application/json", "body": json.dumps({"detail": "Not Found"})}

_state: dict = {}


def _listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def ensure_server() -> None:
    if _state.get("server_checked"):
        return
    if not _listening():
        proc = subprocess.Popen([MOCK_PYTHON, str(HERE / "mock_api.py"), "--port", str(PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        atexit.register(proc.terminate)
        for _ in range(60):
            if _listening():
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("mock server did not start on port %d" % PORT)
    _state["server_checked"] = True


def post(path: str) -> dict:
    ensure_server()
    request = urllib.request.Request(BASE + path, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def reset() -> None:
    post("/mock/reset")


def browser():
    if "browser" not in _state:
        from playwright.sync_api import sync_playwright
        ensure_server()
        SHOTS.mkdir(exist_ok=True)
        _state["pw"] = sync_playwright().start()
        _state["browser"] = _state["pw"].chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        atexit.register(lambda: (_state["browser"].close(), _state["pw"].stop()))
    return _state["browser"]


@contextlib.contextmanager
def open_page(path: str, width: int = 1440, height: int = 900, prepare=None, allow_errors: bool = False):
    """A fresh page on the mock. `prepare(page)` runs before navigation (to install routes).
    Fails the test on an uncaught script error; failed requests the test asked for are not errors."""
    context = browser().new_context(viewport={"width": width, "height": height}, locale="ko-KR", permissions=["microphone"])
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text
            and "ERR_" not in m.text else None)
    if prepare:
        prepare(page)
    page.goto(BASE + path)
    try:
        yield page
        if not allow_errors:
            assert not errors, "script errors on the page: %s" % errors
    finally:
        context.close()


def shot(page, name: str, full: bool = True) -> None:
    page.evaluate("document.fonts && document.fonts.ready")
    page.wait_for_timeout(150)
    page.screenshot(path=str(SHOTS / (name + ".png")), full_page=full)


def no_sideways_scroll(page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 1, "the page scrolls sideways by %d px" % wide


def focus_is_visible(page) -> None:
    """Tab to the next control and check that it draws an outline."""
    page.keyboard.press("Tab")
    style = page.evaluate("(() => { const e = document.activeElement; const s = getComputedStyle(e);"
                          " return [e.tagName, s.outlineStyle, parseFloat(s.outlineWidth)]; })()")
    assert style[1] != "none" and style[2] >= 2, "no visible focus on %s: %s" % (style[0], style)


def main() -> int:
    sys.path.insert(0, str(HERE))
    wanted = sys.argv[1:]
    passed, failed = 0, []
    started = time.time()
    for file in sorted(HERE.glob("test_web_*.py")):
        if file.stem == "test_web_support":
            continue
        module = importlib.import_module(file.stem)
        for name in [n for n in dir(module) if n.startswith("test_")]:
            if wanted and not any(w in file.stem + "::" + name for w in wanted):
                continue
            try:
                getattr(module, name)()
                passed += 1
                print("PASS  %s::%s" % (file.stem, name), flush=True)
            except Exception:
                failed.append(file.stem + "::" + name)
                print("FAIL  %s::%s" % (file.stem, name), flush=True)
                traceback.print_exc()
    print("\n%d passed, %d failed in %.1fs" % (passed, len(failed), time.time() - started))
    for name in failed:
        print("  failed: " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
