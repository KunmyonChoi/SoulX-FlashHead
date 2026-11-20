"""Knowledge retrieval that runs before a reply (definition.knowledge). Ported from v1's KnowledgeClient.

WeKnora's POST /knowledge-search returns the best-matching chunks (vector + keyword recall,
reranked) without any LLM pass, so it is fast enough to run before every reply. The chunks go in
front of the user's message in the prompt of that one turn; they are not kept in the history.
"""
import os
import re
import time

import httpx

from .. import config

# A turn that refers back to what was just said rather than naming a topic of its own. Searched as
# it is ("두 번째 글 더 자세히") it matches unrelated documents, so such a turn reuses the material of
# the turn before.
BACK_REFERENCE = re.compile(
    r"(첫|두|세|네|다섯)\s?번째|[1-5]번|마지막\s?(거|것|글|소식|뉴스|기사)|방금|아까|"
    r"^(그거|그게|그건|그것|그 글|그 소식|그 뉴스|그 기사|그럼|그러면|그래서|왜)\b|"
    r"\b(the (first|second|third|last) one|that one|you just (said|mentioned))\b", re.I)


# The user names the knowledge base: search it before the reply in either mode.
ASKS_KNOWLEDGE = re.compile(r"지식\s?베이스|지식에서|자료에서|저장(된|해 둔|한)\s?(글|자료)|등록(된|한)\s?(글|자료)|"
                            r"knowledge base", re.I)
# The user asks for another source: put no knowledge-base material in front ("자동차 관련 뉴스" was answered
# from a stored document instead of the news, 2026-11-20).
ASKS_ELSEWHERE = re.compile(r"인터넷|웹에서|온라인|구글|뉴스|기사|위키|긱\s?뉴스|날씨|\bweb\b|internet|news|wikipedia",
                            re.I)


def knowledge_first(text, mode):
    """Whether this turn searches the knowledge base before the reply: "before_reply" unless the user asks
    for another source, "tool" only when the user names the knowledge base."""
    if ASKS_KNOWLEDGE.search(text or ""):
        return True
    if mode == "tool" or ASKS_ELSEWHERE.search(text or ""):
        return False
    return True


# WeKnora answers in 0.2-0.35 s as a rule, but about one search in ten takes 1.7 s, and after five
# idle minutes its embedding model is unloaded and the next search takes 3.5-5 s. With a 1.5 s limit
# both showed up as "시간 초과" and the reply went out without reference material.
SEARCH_TIMEOUT = 2.5
WARM_EVERY_S = 240
_warmers = {}   # (url, kb_id) -> thread; one per knowledge base per process


def keep_warm(url, key, kb_id):
    """Starts (once) a background thread that searches the knowledge base every few minutes, so its
    embedding model stays loaded. The first ping runs at once: a session that has just started is
    warm by the time its first question arrives."""
    import threading
    if not (key and kb_id) or (url, kb_id) in _warmers:
        return

    def loop():
        import logging
        log = logging.getLogger("agentkit.runtime")
        while True:
            started = time.perf_counter()
            try:
                httpx.post(url.rstrip("/") + "/knowledge-search", headers={"X-API-Key": key},
                           json={"query": "warm up", "knowledge_base_id": kb_id, "match_count": 1}, timeout=30)
                took = time.perf_counter() - started
                if took > 1.0:
                    log.info(f"Knowledge base warmed up in {took:.1f} s")
            except Exception as e:
                log.warning(f"Knowledge warm-up failed: {e!r}")
            time.sleep(WARM_EVERY_S)

    thread = threading.Thread(target=loop, name="knowledge-warmer", daemon=True)
    _warmers[(url, kb_id)] = thread
    thread.start()


class KnowledgeClient:
    MAX_HITS = 3          # chunks put into the prompt
    SUMMARY_CHARS = 600   # WeKnora's per-document summaries are about 300 characters
    TEXT_CHARS = 900      # body chunks run to 4000 characters; an 8192-token context cannot take many

    def __init__(self, url, key_file, kb_id, name="", timeout=SEARCH_TIMEOUT):
        self.url = url.rstrip("/")
        self.kb_id = kb_id
        self.name = name or kb_id
        self.timeout = timeout
        self.key = None
        self.error = None
        try:
            with open(os.path.expanduser(str(key_file))) as f:
                self.key = f.read().strip() or None
            if not self.key:
                self.error = f"{key_file} is empty"
        except OSError as e:
            self.error = str(e)
        self._client = None

    @property
    def available(self):
        return bool(self.key and self.kb_id)

    async def search(self, query, timeout=None):
        """Returns (hits, ms); hits are dicts with score, title, content and kind, best first."""
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"X-API-Key": self.key or ""})
        started = time.perf_counter()
        response = await self._client.post(
            self.url + "/knowledge-search",
            json={"query": query, "knowledge_base_id": self.kb_id, "match_count": 5},
            timeout=timeout or self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
        hits = [{"score": float(item.get("score") or 0), "title": item.get("knowledge_title") or "",
                 "content": item.get("content") or "", "kind": item.get("chunk_type") or "text"}
                for item in response.json().get("data") or []]
        hits.sort(key=lambda hit: -hit["score"])
        return hits, (time.perf_counter() - started) * 1000

    def context(self, hits, threshold, language="ko"):
        """Returns (prompt text, titles used) for the hits that score at least `threshold`."""
        return knowledge_context(hits, threshold, self.name, language,
                                 self.MAX_HITS, self.SUMMARY_CHARS, self.TEXT_CHARS)

    async def close(self):
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()


def knowledge_context(hits, threshold, name, language="ko", max_hits=3, summary_chars=600, text_chars=900):
    used = [hit for hit in hits if hit["score"] >= threshold][:max_hits]
    if not used:
        return "", []
    parts, titles = [], []
    for index, hit in enumerate(used, 1):
        limit = summary_chars if hit.get("kind") == "summary" else text_chars
        body = re.sub(r"\s+", " ", hit["content"]).strip()[:limit]
        parts.append((f"({index}) Document: " if language == "en" else f"({index}) 문서: ") + f"{hit['title']}\n{body}")
        if hit["title"] not in titles:
            titles.append(hit["title"])
    if language == "en":
        head = (f"[Reference material begins: found in the '{name}' knowledge base. If it is relevant to the "
                "question, base your answer on it; if not, ignore it. Do not invent facts that are not in it. "
                "Do not read out links or document numbers; explain the content in your own spoken words.]")
        tail = "[Reference material ends]"
    else:
        head = (f"[참고 자료 시작: '{name}' 지식베이스에서 찾은 내용입니다. 질문과 관련이 있으면 이 내용을 근거로 "
                "답하고, 관련이 없으면 무시하세요. 자료에 없는 사실은 지어내지 마세요. 링크나 문서 번호는 읽지 "
                "말고 내용을 말로 풀어서 설명하세요.]")
        tail = "[참고 자료 끝]"
    return head + "\n" + "\n\n".join(parts) + "\n" + tail, titles


def build_knowledge(settings):
    """The retrieval client for a definition's knowledge settings, or None (kind "none", no key)."""
    if settings is None or settings.kind == "none":
        return None
    if settings.kind == "weknora":
        client = KnowledgeClient(config.WEKNORA_URL, config.WEKNORA_KEY_FILE, settings.kb_id, settings.name)
        if client.available:
            keep_warm(client.url, client.key, client.kb_id)
        return client if client.available else None
    return None
