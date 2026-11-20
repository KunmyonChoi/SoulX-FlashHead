"""A short note on where the conversation stands, kept by code from the history (not by the model).

Each turn the model gets, in front of the user's words: what was last looked up (tool, arguments, the first
items it returned) and the question the agent last asked. Short follow-ups are then read in that frame:
"자세히 알아봐 줘" after a news search is about that news, "그렇게 해줘" agrees to what was offered.
Found in a real v1 conversation (2026-11-20), where each such turn was read on its own.

Built from the history every turn, so it can never drift from what really happened; nothing is stored.
"""
import json
import re

HANDOFF = "handoff_to_human"
MAX_TURNS_BACK = 4      # a lookup older than this many customer turns is no longer "what we are doing"
MAX_ITEMS = 5
ITEM = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|[-•·]|\[\d{1,2}\])\s*(.+)$")


def _customer(message):
    """A user-role message the customer said; the engine's own notes ("(대화가 시작되었습니다...)") are not."""
    return message.get("role") == "user" and not str(message.get("content") or "").lstrip().startswith("(")


def _items(result):
    """The first lines of a numbered or bulleted list in a tool result, shortened."""
    found = []
    for raw in str(result or "").splitlines():
        match = ITEM.match(raw)
        if match:
            found.append(re.sub(r"\s+", " ", match.group(1)).strip()[:70])
        if len(found) >= MAX_ITEMS:
            break
    return found


def _last_sentence(text):
    parts = [p for p in re.split(r"(?<=[.?!])\s+", str(text or "").strip()) if p]
    return parts[-1] if parts else ""


def _arguments(arguments):
    shown = {k: v for k, v in (arguments or {}).items() if v not in (None, "", [], {})}
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items())


def situation(history, language="ko"):
    """The note for the turn about to be answered, or "" when nothing has happened yet."""
    lookup, results, turns_back = None, [], 0
    asked = ""
    seen_reply = False
    # what the agent did before the user first spoke (its opening: menu, cart) is not part of the flow
    first_user = next((i for i, m in enumerate(history) if _customer(m)), len(history))
    for index in range(len(history) - 1, first_user - 1, -1):
        message = history[index]
        role = message.get("role")
        if _customer(message):
            turns_back += 1
            if turns_back > MAX_TURNS_BACK:
                break
        elif role == "assistant" and message.get("content") and not seen_reply:
            seen_reply = True
            sentence = _last_sentence(message["content"])
            if sentence.endswith(("?", "？")):
                asked = sentence[:120]
        elif role == "assistant" and message.get("tool_calls") and lookup is None:
            calls = [c.get("function", c) for c in message["tool_calls"]]
            calls = [c for c in calls if c.get("name") != HANDOFF]
            if calls:
                lookup = calls
                results = [m.get("content") for m in history[index + 1:index + 1 + len(message["tool_calls"])]
                           if m.get("role") == "tool"]
        if lookup is not None and seen_reply:
            break
    if not lookup and not asked:
        return ""
    lines = []
    if lookup:
        done = "; ".join(f"{c.get('name')}({_arguments(c.get('arguments'))})" for c in lookup)
        items = [item for result in results for item in _items(result)][:MAX_ITEMS]
        if language == "en":
            lines.append(f"- Last looked up: {done}" + (" -> " + " / ".join(items) if items else ""))
        else:
            lines.append(f"- 최근에 한 조회: {done}" + (" → 결과: " + " / ".join(items) if items else ""))
    if asked:
        lines.append(f"- Last question to the user: \"{asked}\"" if language == "en" else
                     f"- 직전에 사용자에게 물은 것: \"{asked}\"")
    if language == "en":
        head = ("(Where the conversation stands. Read the user's words within it: short follow-ups like "
                "\"tell me more\", \"that one\", \"do that\", \"OK\" usually continue it.)")
    else:
        head = ("(지금 대화의 흐름입니다. 사용자의 말을 이 흐름 안에서 이해하세요. '자세히', '그거', '그렇게 해줘', "
                "'좋아'처럼 짧은 말은 대개 이 흐름을 잇는 말입니다.)")
    return head + "\n" + "\n".join(lines)
