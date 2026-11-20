"""The one Ollama client of v2 (runtime, evaluation and the builder all use it)."""
import json

import httpx

from . import config


class LLM:
    def __init__(self, url=None, model=None):
        self.url = (url or config.LLM_URL).rstrip("/")
        self.model = model or config.LLM_MODEL
        self._capabilities = {}

    async def supports(self, capability, model=None):
        """capability: "tools" or "thinking"."""
        model = model or self.model
        if model not in self._capabilities:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(self.url + "/api/show", json={"model": model})
                    self._capabilities[model] = response.json().get("capabilities", [])
            except Exception:
                return False
        return capability in self._capabilities[model]

    async def _body(self, messages, model, stream, num_predict, num_ctx, temperature):
        body = {"model": model, "messages": messages, "stream": stream, "keep_alive": "30m",
                "options": {"num_predict": num_predict, "num_ctx": num_ctx}}
        if temperature is not None:
            body["options"]["temperature"] = temperature
        if await self.supports("thinking", model):
            body["think"] = False  # replies are spoken or judged at once: no thinking phase
        return body

    async def complete(self, messages, *, model=None, schema=None, num_predict=800, num_ctx=8192,
                       temperature=None, timeout=90):
        """A whole reply at once. With `schema` (a JSON Schema) the reply is JSON text matching it."""
        model = model or self.model
        body = await self._body(messages, model, False, num_predict, num_ctx, temperature)
        if schema:
            body["format"] = schema
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as client:
            response = await client.post(self.url + "/api/chat", json=body)
            if response.status_code != 200:
                raise RuntimeError(response.text[:300])
            return response.json().get("message", {}).get("content", "")

    async def complete_json(self, messages, schema, **kwargs):
        """complete() with a schema, parsed. Raises ValueError if the model returns no valid JSON."""
        text = await self.complete(messages, schema=schema, **kwargs)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"model did not return JSON: {text[:200]!r}") from e

    async def stream(self, messages, *, model=None, tools=None, num_predict=1500, num_ctx=8192,
                     temperature=None):
        """Yields ("text", delta), ("tool", {"name", "arguments"}) and finally ("stats", {...})."""
        model = model or self.model
        body = await self._body(messages, model, True, num_predict, num_ctx, temperature)
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
            async with client.stream("POST", self.url + "/api/chat", json=body) as response:
                if response.status_code != 200:
                    raise RuntimeError((await response.aread()).decode(errors="replace")[:300])
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message", {})
                    if message.get("content"):
                        yield "text", message["content"]
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        if function.get("name"):
                            yield "tool", {"name": function["name"], "arguments": function.get("arguments") or {}}
                    if data.get("done"):
                        yield "stats", {"total_s": data.get("total_duration", 0) / 1e9,
                                        "load_s": data.get("load_duration", 0) / 1e9,
                                        "prompt_tokens": data.get("prompt_eval_count", 0),
                                        "output_tokens": data.get("eval_count", 0)}
                        break
