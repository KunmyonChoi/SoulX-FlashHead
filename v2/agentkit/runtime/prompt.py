"""Prompt assembly: everything the model is told comes from the AgentDefinition (R-DEF-5).

The order of the system prompt is fixed by ARCHITECTURE.md section 5:
persona, reply length, speaking rules, flow, prohibitions, handoff, tool rules, date and calendar.
The minute-by-minute clock is not in here: it is prefixed to the newest user message, so the
system prompt (and with it Ollama's prompt cache) changes only once a day.
"""
import re
from datetime import timedelta

HANDOFF_TOOL = "handoff_to_human"   # the tool whose execution hands the conversation over (ARCHITECTURE 4, 5)

# Each entry: Korean instruction, English instruction, LLM output cap (tokens), LLM context (tokens).
# Ported from v1 (REPLY_LENGTHS). v1's "short" had no line because its default persona carried the
# limit; a v2 persona need not, so "short" says it too.
REPLY_LENGTHS = {
    "short": (
        "답변 길이: 한두 문장, 길어도 세 문장 이내로 짧게 답하세요.",
        "Reply length: answer in one or two sentences, three at most.",
        1500, 8192),
    "normal": (
        "답변 길이: 서너 문장으로 답하세요.",
        "Reply length: answer in three or four sentences.",
        1500, 8192),
    "long": (
        "답변 길이: 예닐곱 문장으로 이유나 예시를 들어 충분히 답하세요. "
        "목록처럼 나열하지 말고 말하듯 이어서 설명하세요.",
        "Reply length: answer fully in six or seven sentences, with a reason or an example. "
        "Do not enumerate like a list; explain it the way you would say it.",
        1500, 8192),
    "explain": (
        "답변 길이: 열 문장 이상으로 차근차근 자세히 설명하세요. "
        "배경, 핵심 내용, 예시 순서로 풀어 가되, 목록이나 번호 없이 말하듯 이어서 설명하세요.",
        "Reply length: explain step by step in ten sentences or more: background, the main points, "
        "then an example. No lists or numbering; explain it the way you would say it.",
        2500, 16384),
}
# Said in a turn, these change the length of that one reply: "자세히 설명해줘", "짧게 말해줘".
ASK_LONGER = re.compile(
    r"(자세히|자세하게|상세히|상세하게|길게|구체적으로|깊이)|\b(in (more )?detail|at length|elaborate)\b", re.I)
ASK_SHORTER = re.compile(
    r"(짧게|간단히|간단하게|간략히|간략하게|한마디로|요약해)|\b(briefly|in short|keep it short)\b", re.I)


def reply_length(text, setting):
    """The length for one reply: the setting, unless the user asked for more or less in this turn."""
    setting = setting if setting in REPLY_LENGTHS else "short"
    if ASK_SHORTER.search(text or ""):
        return "short"
    if ASK_LONGER.search(text or ""):
        # one step up, but never into "explain" from below: its larger context would reload the model
        return "explain" if setting == "explain" else "long"
    return setting


HANGUL = re.compile("[가-힣]")


def text_language(text):
    """"ko" if the text has any Hangul, else "en"."""
    return "ko" if HANGUL.search(text or "") else "en"


# Characters a TTS would read aloud or stumble on: markdown markers and emoji.
UNSPEAKABLE = re.compile("[*#`_~>|\\[\\]\U0001F000-\U0001FAFF☀-➿️]")
PIECE_END = re.compile(r"[.!?。！？](?=\s)|\n")


def speakable(text):
    return UNSPEAKABLE.sub("", text or "").strip()


def take_piece(buffer, min_chars):
    """Cut a speakable piece off the front of streamed LLM text, ending at a sentence boundary."""
    for match in PIECE_END.finditer(buffer):
        if len(buffer[:match.end()].strip()) >= min_chars:
            return buffer[:match.end()], buffer[match.end():]
    return None, buffer


# ---------------------------------------------------------------------------------- date and clock

WEEKDAYS_KO = "월화수목금토일"
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December")
CALENDAR_DAYS = 7


def _date_ko(day):
    return f"{day.month}월 {day.day}일 {WEEKDAYS_KO[day.weekday()]}요일"


def _date_en(day):
    return f"{WEEKDAYS_EN[day.weekday()]}, {MONTHS_EN[day.month - 1]} {day.day}"


def clock_text(now, language="ko"):
    """The part that changes every minute: prefixed to the newest user message."""
    hour = now.hour % 12 or 12
    if language == "en":
        return f"[Current time: {_date_en(now)}, {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}]"
    return f"[지금 시각: {_date_ko(now)} {'오전' if now.hour < 12 else '오후'} {hour}시 {now.minute}분]"


def calendar_text(now, language="ko"):
    """The part that changes once a day: today's date and the days ahead, date first so the model
    looks a weekday up instead of counting to it."""
    days = [now + timedelta(days=i) for i in range(CALENDAR_DAYS + 1)]
    if language == "en":
        labels = {0: "today", 1: "tomorrow"}
        calendar = ", ".join(f"{MONTHS_EN[d.month - 1]} {d.day} is a {WEEKDAYS_EN[d.weekday()]}"
                             + (f" ({labels[i]})" if i in labels else "") for i, d in enumerate(days))
        return (f"Today is {_date_en(now)}, {now.year}. The current time is written in square brackets in front "
                f"of the user's latest message; answer questions about the date or time from it directly.\n"
                f"Calendar: {calendar}.")
    labels = {0: "오늘", 1: "내일", 2: "모레"}
    calendar = ", ".join(f"{d.month}월 {d.day}일은 {WEEKDAYS_KO[d.weekday()]}요일"
                         + (f"({labels[i]})" if i in labels else "") for i, d in enumerate(days))
    return (f"오늘은 {now.year}년 {_date_ko(now)}입니다. 지금 시각은 사용자의 말 앞에 대괄호로 적혀 있습니다. "
            f"지금 시각과 날짜는 이 정보로 바로 답하세요.\n달력: {calendar}.")


# ---------------------------------------------------------------------------------- system prompt

TEXT = {
    "ko": {
        "speaking": "말하기 규칙: 답변은 그대로 음성으로 읽힙니다. 마크다운, 목록, 번호 매기기, 이모지, "
                    "괄호 설명을 쓰지 말고 말하듯 문장으로 답하세요. 사용자의 말은 음성 인식을 거친 글이라 틀릴 수 "
                    "있습니다. 말이 이상하면 방금 나눈 이야기에서 가장 그럴듯한 뜻을 짐작해 짧게 확인하세요(예: 오전과 "
                    "오후를 물은 뒤 '우후'라고 들리면 '오후 말씀이시죠?'). 다시 말해 달라고만 되풀이하지 마세요. 무언가를 "
                    "해 드릴지 물은 뒤 '그래', '응'처럼 짧게 동의하면 다시 묻지 말고 그 일을 하세요.",
        "flow": "반드시 지킬 순서:",
        "prohibitions": "절대 하지 않는 일:",
        "handoff": "상담원에게 넘기는 경우:",
        "handoff_call": "위 경우 중 하나에 해당하면 직접 처리하려 하지 말고 바로 {tool} 도구를 호출하세요. "
                        "호출한 뒤 고객에게 상담원에게 연결한다고 한 문장으로 알리세요. "
                        "도구를 호출하지 않고 연결했다고 말하지 마세요.",
        "handoff_say": "위 경우 중 하나에 해당하면 직접 처리하려 하지 말고, 상담원의 도움이 필요하다고 고객에게 알리세요.",
        "tools": "도구 사용 규칙:",
        "tools_rules": [
            "사실을 추측하거나 지어내지 말고, 필요한 정보는 도구를 호출해 확인하세요. "
            "도구를 호출하지 않고 확인하거나 처리한 것처럼 말하지 마세요.",
            "도구에 넣을 값을 고객이 아직 말하지 않았다면 지어내지 말고 먼저 물어보세요.",
            "도구 결과에 없는 내용은 덧붙이지 마세요. 도구가 실패하면 결과에 적힌 안내를 고객에게 전하세요.",
        ],
        "write": "변경 도구({names})는 두 단계로 실행됩니다. 처음 호출하면 아무것도 실행되지 않고 확인 문장이 "
                 "돌아옵니다. 그 확인 문장을 고객에게 그대로 읽어 주고 동의를 구하세요. 고객이 다음 말에서 "
                 "동의하면 같은 인자로 그 도구를 다시 호출하세요. 그때 실제로 실행됩니다. 고객이 동의하지 "
                 "않으면 다시 호출하지 마세요. 실행되었다는 결과를 받기 전에는 처리되었다고 말하지 마세요.",
        "screen": "화면: 고객은 화면으로 {panels}을 보고 있습니다. 도구 결과는 화면에 그대로 표시됩니다. 메뉴 전체나 "
                  "주문 내역 전체를 말로 읽지 말고, 바뀐 것과 다음에 할 일만 짧게 말하세요.",
        "screen_touch": "고객은 말하는 대신 화면을 눌러 직접 처리할 수도 있습니다. 그런 동작은 괄호 안의 문장으로 "
                        "전달되며 이미 처리된 것입니다. 같은 도구를 다시 호출하지 말고 한 문장으로 짧게 확인만 하세요.",
        "panels": {"menu": "메뉴", "cart": "주문 내역"},
        "panels_join": "와 ",
        "knowledge": "사용자의 말 앞에 '{name}' 지식베이스에서 찾은 '참고 자료'가 붙어 있으면, 질문과 관련이 "
                     "있을 때 그 자료를 근거로 답하세요.",
        "knowledge_tool": "'{name}' 지식베이스는 미리 찾아 주지 않습니다. 사용자가 지식베이스나 저장된 자료를 원할 때, "
                          "또는 앞서 지식베이스에서 소개한 자료를 더 물을 때 search_knowledge를 부르세요. 검색어는 "
                          "사용자의 말을 그대로 쓰지 말고 지금 이야기하는 주제로 정하세요. 뉴스, 인터넷, 최신 소식을 "
                          "원하면 지식베이스를 쓰지 말고 그에 맞는 도구를 쓰세요.",
    },
    "en": {
        "speaking": "Speaking rules: your reply is read aloud as it is. Use no markdown, lists, numbering, emoji "
                    "or parenthetical asides; answer in spoken sentences. The user's words come from speech recognition "
                    "and may be wrong: if they make no sense, guess the most likely meaning from what was just said and "
                    "confirm it briefly instead of only asking them to repeat. If you offered to do something and they "
                    "answer with a short yes, do it without asking again.",
        "flow": "Order you must always follow:",
        "prohibitions": "Things you never do:",
        "handoff": "When to hand over to a human agent:",
        "handoff_call": "If one of these applies, do not try to handle it yourself: call the {tool} tool at once, "
                        "then tell the customer in one sentence that you are connecting them to a human agent. "
                        "Never say you have connected them without calling the tool.",
        "handoff_say": "If one of these applies, do not try to handle it yourself: tell the customer that a "
                       "human agent is needed.",
        "tools": "Rules for using tools:",
        "tools_rules": [
            "Do not guess or invent facts: call a tool to check what you need. "
            "Never speak as if you had checked or done something without calling the tool.",
            "If the customer has not yet given a value a tool needs, do not make it up: ask for it first.",
            "Add nothing that is not in a tool result. If a tool fails, pass on the guidance in its result.",
        ],
        "write": "Tools that change something ({names}) run in two steps. The first call executes nothing and "
                 "returns a confirmation sentence. Read that sentence to the customer as it is and ask for their "
                 "consent. If the customer agrees in their next message, call the tool again with the same "
                 "arguments; only then is it executed. If they do not agree, do not call it again. Never say it "
                 "is done before you have received the result of the execution.",
        "screen": "Screen: the customer is looking at {panels} on a screen. Tool results are shown there as they "
                  "are. Do not read the whole menu or the whole order aloud; say only what changed and what "
                  "comes next, briefly.",
        "screen_touch": "The customer may also do things by touching the screen instead of saying them. Such a "
                        "touch arrives as a sentence in parentheses and has already been carried out: do not call "
                        "the same tool again, just acknowledge it in one short sentence.",
        "panels": {"menu": "the menu", "cart": "their order"},
        "panels_join": " and ",
        "knowledge": "If 'reference material' from the '{name}' knowledge base is placed in front of the user's "
                     "message, base your answer on it when it is relevant to the question.",
        "knowledge_tool": "The '{name}' knowledge base is not searched for you in advance. Call search_knowledge "
                          "when the user wants the knowledge base or stored material, or asks more about material "
                          "from it. Choose the query from the topic being discussed, not the user's words as they "
                          "are. For news, the internet or current events use the matching tool instead.",
    },
}
# Added after a persona written in another language, when this turn is in English (language "auto").
ENGLISH_OVERRIDE = (
    "IMPORTANT: this conversation is in English. Disregard any instruction above about speaking Korean or "
    "avoiding English words: reply only in natural spoken English, even when rules, tool results or "
    "reference material are written in Korean."
)


def _bullets(lines):
    return "\n".join(f"- {line.strip()}" for line in lines if line and line.strip())


def build_system_prompt(definition, *, language="ko", length=None, now=None, contracts=None,
                        knowledge_name="", knowledge_mode="before_reply"):
    """The system prompt of one reply.

    language: "ko" or "en", the language of this turn (already resolved from "auto").
    length:   the reply length of this turn (see reply_length()); default the definition's.
    now:      aware datetime; needed only with definition.live_clock.
    contracts: {tool name: ToolContract} of the tools this reply may call; empty if it may call none.
    """
    language = "en" if language == "en" else "ko"
    text = TEXT[language]
    contracts = contracts or {}
    length = length if length in REPLY_LENGTHS else (
        definition.reply_length if definition.reply_length in REPLY_LENGTHS else "short")
    parts = [definition.persona.strip()]                                                   # 1
    if language == "en" and definition.language != "en":
        parts.append(ENGLISH_OVERRIDE)
    parts.append(REPLY_LENGTHS[length][1 if language == "en" else 0])                      # 2
    parts.append(text["speaking"])                                                         # 3
    panels = list(getattr(getattr(definition, "screen", None), "panels", None) or [])
    if panels:                                                                             # 3a (DISPLAY.md)
        screen = text["screen"].format(panels=text["panels_join"].join(text["panels"].get(p, p) for p in panels))
        if getattr(definition, "actions", None):
            screen += " " + text["screen_touch"]
        parts.append(screen)
    if definition.flow:                                                                    # 4
        parts.append(text["flow"] + "\n" + _bullets(rule.rule for rule in definition.flow))
    if definition.prohibitions:                                                            # 5
        parts.append(text["prohibitions"] + "\n" + _bullets(definition.prohibitions))
    if definition.handoff:                                                                 # 6
        call = (text["handoff_call"].format(tool=HANDOFF_TOOL) if HANDOFF_TOOL in contracts
                else text["handoff_say"])
        parts.append(text["handoff"] + "\n" + _bullets(rule.when for rule in definition.handoff) + "\n" + call)
    rules = []                                                                             # 7
    if contracts:
        rules.extend(text["tools_rules"])
        two_phase = [name for name, contract in contracts.items()
                     if getattr(contract, "effect", "read") == "write"
                     and getattr(contract, "confirm_template", None)]
        if two_phase:
            rules.append(text["write"].format(names=", ".join(two_phase)))
    if knowledge_name:
        rules.append(text["knowledge"].format(name=knowledge_name))
        if knowledge_mode == "tool" and "search_knowledge" in (contracts or {}):
            rules.append(text["knowledge_tool"].format(name=knowledge_name))
    if rules:
        parts.append(text["tools"] + "\n" + _bullets(rules))
    if definition.live_clock and now is not None:                                          # 8
        parts.append(calendar_text(now, language))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------------- fixed lines

LINES = {
    "ko": {
        "filler": "잠시만요, 확인해 볼게요.",
        "handed_off": "상담원에게 연결되었습니다. 잠시만 기다려 주세요.",
        "fallback": "죄송합니다. 지금은 답변을 드리기 어렵습니다. 다시 한 번 말씀해 주시겠어요?",
        "unknown_tool": "'{name}' 도구는 이 상담에서 쓸 수 없습니다. 쓸 수 있는 도구: {names}.",
        "tool_crash": "도구 호출에 실패했습니다. 지금은 확인이 어렵다고 고객에게 알리세요.",
        "confirm": "(시스템 안내: 아직 아무것도 실행되지 않았습니다. 고객에게 다음 확인 문장을 그대로 읽어 주고 "
                   "동의를 구하세요: \"{text}\" 고객이 다음 말에서 동의하면 같은 인자로 {name}을 다시 호출하세요. "
                   "지금은 다시 호출하지 마세요.)",
        "failed": "[실패: {error}] {text}",
        "handoff_done": "(시스템 안내: 상담원 연결이 접수되었습니다. 고객에게 상담원에게 연결한다고 한 문장으로 알리세요.)",
        "handoff_done_say": "(시스템 안내: 상담원 연결이 접수되었습니다. 고객에게 다음 문장을 그대로 말하세요: \"{text}\")",
        "continue": " 이미 말한 부분은 반복하지 말고 그 뒤를 이어서 말하세요.",
        "question": "질문: ",
        "opening": "(대화가 시작되었습니다. 고객이 방금 도착했습니다.)",
        "touch": "화면에서 {name} 동작을 했습니다",
        "touch_done": "(시스템 안내: 고객이 화면을 눌러 직접 한 동작이며 이미 처리되었습니다. 같은 도구를 다시 호출하지 "
                      "마세요. 바뀐 것만 한 문장으로 짧게 확인하세요.)",
        "touch_proposed": "(시스템 안내: 고객이 화면을 눌러 요청했고, 아직 실행하지 않았습니다. 확인할 내용이 화면에 떠 있습니다. "
                          "같은 도구를 다시 호출하지 말고, 화면의 내용을 확인하고 단추를 누르거나 '응'이라고 말하면 된다고 한 문장으로 "
                          "말하세요.)",
        "touch_failed": "(시스템 안내: 고객이 화면을 눌렀지만 처리되지 않았습니다. 다시 호출하지 말고, 위 안내를 "
                        "고객에게 한두 문장으로 전하세요.)",
        "confirm_open": "(시스템 안내: 고객의 답이 동의인지 거절인지 분명하지 않습니다. 요청은 취소되지 않고 그대로 "
                        "기다리고 있습니다. \"접수하지 않았다\"거나 \"취소했다\"고 말하지 마세요. 고객의 말에 한 문장으로 "
                        "답한 뒤, 이 확인 문장을 그대로 다시 물으세요: \"{text}\" 도구는 다시 호출하지 마세요.)",
    },
    "en": {
        "filler": "One moment, let me check.",
        "handed_off": "You have been connected to a human agent. Please hold on.",
        "fallback": "I'm sorry, I can't answer that right now. Could you say it once more?",
        "unknown_tool": "The tool '{name}' is not available in this conversation. Available tools: {names}.",
        "tool_crash": "The tool call failed. Tell the customer that this cannot be checked right now.",
        "confirm": "(System note: nothing has been executed yet. Read this confirmation sentence to the customer "
                   "as it is and ask for their consent: \"{text}\" If the customer agrees in their next message, "
                   "call {name} again with the same arguments. Do not call it again now.)",
        "failed": "[failed: {error}] {text}",
        "handoff_done": "(System note: the handover is registered. Tell the customer in one sentence that you are "
                        "connecting them to a human agent.)",
        "handoff_done_say": "(System note: the handover is registered. Say this to the customer as it is: \"{text}\")",
        "continue": " Do not repeat what you have already said; continue after it.",
        "question": "Question: ",
        "opening": "(The conversation has started. The customer has just arrived.)",
        "touch": "did {name} on the screen",
        "touch_done": "(System note: the customer did this by touching the screen and it has already been carried "
                      "out. Do not call the tool again. Acknowledge what changed in one short sentence.)",
        "touch_proposed": "(System note: the customer asked for this on the screen; it has not been carried out. What "
                          "to confirm is on the screen. Do not call the tool again; say in one sentence that they can "
                          "press the button or say yes.)",
        "touch_failed": "(System note: the customer touched the screen but it was not carried out. Do not call the "
                        "tool again; pass on the guidance above in a sentence or two.)",
        "confirm_open": "(System note: it is not clear whether the customer agreed or declined. The request is not "
                        "cancelled; it is still waiting. Do not say it was not done or was cancelled. Answer what the "
                        "customer said in one sentence, then ask this confirmation sentence again as it is: \"{text}\" "
                        "Do not call the tool again.)",
    },
}


def line(key, language="ko", **values):
    template = LINES["en" if language == "en" else "ko"][key]
    return template.format(**values) if values else template


PLACEHOLDER = re.compile(r"\{(\w+)\}")


def touch_sentence(action, arguments, language="ko", data=None):
    """How a touch on the screen reads as the customer's turn: ClientAction.say with "{field}" filled
    from the arguments (and, for names only the system knows, from the top of the tool's result),
    in parentheses so that nobody takes it for something that was said."""
    values = {}
    if isinstance(data, dict):
        for value in data.values():       # one level down first, so the top level wins
            if isinstance(value, dict):
                values.update({k: v for k, v in value.items() if isinstance(v, (str, int, float))})
        values.update({k: v for k, v in data.items() if isinstance(v, (str, int, float))})
    values.update({k: v for k, v in (arguments or {}).items() if isinstance(v, (str, int, float))})
    template = (getattr(action, "say", "") or "").strip() or line("touch", language, name=action.name)
    sentence = PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "")), template)
    sentence = re.sub(r"\s{2,}", " ", sentence).strip()
    if not (sentence.startswith("(") and sentence.endswith(")")):
        sentence = f"({sentence})"
    return sentence
