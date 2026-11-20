"""
The dummy shop over HTTP, so the "http" adapter has something to talk to.

    cd v2 && python -m agentkit.dummy.shop_server --port 7895

    POST /shop/{method}            {"arguments": {...}, "context": {"session_id": ...}}
    POST /shop/{method}/preview    same body; what a write would do, nothing is changed
    GET  /shop/records?session_id= refunds and handoffs made so far
    POST /shop/end_session         {"session_id": ...}
    POST /shop/inject              {"delay_s": .., "fail": .., "fail_times": ..}   ({} clears)
    GET  /health

Answers are {"ok", "text", "data", "error_type"} with status 200 also for refusals: a refusal is an
answer of the system. An injected outage ("fail": "unavailable") answers 503.
"""
import argparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .shop import METHODS, ShopSystem, ShopUnavailable


def create_app(shop=None):
    shop = shop or ShopSystem()
    app = FastAPI(title="dummy shop")
    app.state.shop = shop

    @app.get("/health")
    async def health():
        return {"ok": True, "system": "shop", "methods": list(METHODS)}

    @app.get("/shop/records")
    async def records(session_id: str | None = None, kind: str | None = None):
        return {"records": shop.records(session_id, kind)}

    @app.post("/shop/end_session")
    async def end_session(body: dict):
        shop.end_session(str(body.get("session_id") or "default"))
        return {"ok": True}

    @app.post("/shop/inject")
    async def inject(body: dict):
        shop.inject(**body)
        return {"ok": True, "options": shop.options}

    @app.post("/shop/{method}/preview")
    async def preview(method: str, body: dict):
        try:
            out = await shop.preview(method, body.get("arguments") or {}, body.get("context") or {})
        except ShopUnavailable as e:
            return JSONResponse({"ok": False, "text": str(e), "error_type": "system_error"}, status_code=503)
        return out or {"ok": True, "text": "", "data": {}, "error_type": None}

    @app.post("/shop/{method}")
    async def call(method: str, body: dict):
        try:
            return await shop.call(method, body.get("arguments") or {}, body.get("context") or {})
        except ShopUnavailable as e:
            return JSONResponse({"ok": False, "text": str(e), "error_type": "system_error"}, status_code=503)

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="dummy shop over HTTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7895)
    parser.add_argument("--delay-s", type=float, default=0.0, help="delay added to every call")
    parser.add_argument("--fail", default=None, help="unavailable | system_error | timeout | <error type>")
    parser.add_argument("--fail-times", type=int, default=None, help="fail only the first n calls per method and session")
    args = parser.parse_args()
    options = {}
    if args.delay_s:
        options["delay_s"] = args.delay_s
    if args.fail:
        options["fail"] = args.fail
    if args.fail_times is not None:
        options["fail_times"] = args.fail_times
    uvicorn.run(create_app(ShopSystem(options=options)), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
