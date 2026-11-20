"""Generates the Genitor deck that explains the real-time avatar's architecture."""
import sys

BG, INK, MUTED, LINE, CARD = "#F5F2EA", "#14213D", "#5C6470", "#DED8CA", "#FFFFFF"
CORAL, TEAL, NAVY2, SAND = "#E4572E", "#0F8B8D", "#22335C", "#EBE5D6"
SANS = "'IBM Plex Sans KR',sans-serif"
MONO = "'JetBrains Mono',monospace"

slides = []


class Slide:
    def __init__(self, bg=BG, transition="fade"):
        self.bg, self.transition = bg, transition
        self.items, self.groups, self.notes = [], set(), ""

    def anim(self, group, delay=0, dur=380, kind="fadeIn", direction=None):
        """First use of a group opens a step (click, or auto for groups named auto*)."""
        if group is None:
            return ""
        attrs = f' data-anim="{kind}" data-anim-duration="{dur}"'
        if direction:
            attrs += f' data-anim-dir="{direction}"'
        if group not in self.groups:
            self.groups.add(group)
            trigger = "auto" if group.startswith("auto") else "click"
            attrs += f' data-anim-trigger="{trigger}" data-anim-name="{group}"'
            if delay:
                attrs += f' data-anim-delay="{delay}"'
        else:
            attrs += f' data-anim-trigger="with" data-anim-ref="{group}"'
            if delay:
                attrs += f' data-anim-delay="{delay}"'
        return attrs

    def box(self, x, y, w, h, fill=CARD, border=LINE, radius=18, g=None, delay=0, extra="", dur=380):
        style = f"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;background:{fill};"
        if border:
            style += f"border:1px solid {border};"
        style += f"border-radius:{radius}px;{extra}"
        self.items.append(f'<div{self.anim(g, delay, dur)} style="{style}"></div>')

    def text(self, x, y, w, h, html, size=28, weight=400, color=INK, lh=1.45, font=SANS, align="left",
             g=None, delay=0, dur=380, extra="", kind="fadeIn", direction=None):
        style = (f"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;font-family:{font};"
                 f"font-size:{size}px;font-weight:{weight};color:{color};line-height:{lh};text-align:{align};{extra}")
        self.items.append(f'<div{self.anim(g, delay, dur, kind, direction)} style="{style}">{html}</div>')

    def line(self, x1, y1, x2, y2, color=INK, width=3, g=None, delay=0, arrow=True, dash=False):
        """A straight connector in its own small SVG (canvas coordinates kept through the viewBox)."""
        pad = 14
        left, top = min(x1, x2) - pad, min(y1, y2) - pad
        w, h = abs(x2 - x1) + 2 * pad, abs(y2 - y1) + 2 * pad
        head = ""
        if arrow:
            import math
            angle = math.atan2(y2 - y1, x2 - x1)
            size = 13
            p1 = (x2 - size * math.cos(angle - 0.45), y2 - size * math.sin(angle - 0.45))
            p2 = (x2 - size * math.cos(angle + 0.45), y2 - size * math.sin(angle + 0.45))
            head = (f'<path d="M {p1[0]:.1f} {p1[1]:.1f} L {x2} {y2} L {p2[0]:.1f} {p2[1]:.1f}" '
                    f'stroke="{color}" stroke-width="{width}" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
        dashes = ' stroke-dasharray="8 8"' if dash else ""
        self.items.append(
            f'<svg{self.anim(g, delay, 240)} width="{w}" height="{h}" viewBox="{left} {top} {w} {h}" fill="none" '
            f'style="position:absolute;left:{left}px;top:{top}px;width:{w}px;height:{h}px;">'
            f'<path d="M {x1} {y1} L {x2} {y2}" stroke="{color}" stroke-width="{width}" stroke-linecap="round"{dashes}/>'
            f'{head}</svg>')

    def raw(self, html):
        self.items.append(html)

    def header(self, kicker, title):
        self.text(120, 84, 900, 34, kicker, size=22, weight=600, color=CORAL, font=MONO, lh=1.5, g="autoh",
                  extra="letter-spacing:2px;")
        self.text(120, 124, 1680, 84, title, size=58, weight=700, color=INK, lh=1.4, g="autoh", delay=80, dur=520)
        self.box(120, 220, 1680, 3, fill=INK, border=None, radius=0)

    def html(self, active):
        cls = "slide active" if active else "slide"
        notes = "\n\n".join("      " + p.strip() for p in self.notes.strip().split("\n\n"))
        body = "\n    ".join(self.items)
        return (f'  <div class="{cls}" data-transition="{self.transition}" '
                f'style="width:1920px;height:1080px;background:{self.bg};font-family:{SANS};">\n    {body}\n'
                f'    <script type="text/plain" class="fe-notes">\n{notes}\n    </script>\n  </div>')


def new(**kw):
    s = Slide(**kw)
    slides.append(s)
    return s


# ---- 1. cover ------------------------------------------------------------------------------------
s = new(bg=INK)
s.box(120, 150, 96, 10, fill=CORAL, border=None, radius=5)
s.text(120, 196, 1200, 36, "REALTIME AVATAR · ARCHITECTURE", size=24, weight=600, color="#9FB0D0", font=MONO,
       lh=1.5, g="auto1", extra="letter-spacing:4px;")
s.text(120, 300, 1500, 290, "실시간 대화 아바타<br>아키텍처와 흐름 제어", size=112, weight=700, color="#F5F2EA",
       lh=1.28, g="auto1", delay=120, dur=600)
s.text(120, 640, 1400, 104, "마이크 소리가 답하는 얼굴이 되기까지, 서버 안에서 일어나는 일과<br>말 끊기·미리 답하기·정보 조회를 다루는 제어 로직",
       size=34, color="#C8D1E3", lh=1.5, g="auto1", delay=380, dur=450)
s.text(120, 900, 1400, 40, "realtime_mic_app.py · realtime_tools.py · realtime_mic_client.html", size=24, color="#7F8FB0",
       font=MONO, lh=1.5, g="auto1", delay=560)
s.notes = """
이 발표는 실시간 대화 아바타가 지금 어떻게 만들어져 있는지를 설명합니다. 코드 세 개 파일을 직접 읽고 정리한 내용입니다.

앞 절반은 구조입니다. 어떤 프로세스가 있고, 한 번의 대화에서 데이터가 어디로 흐르는지 봅니다.

뒤 절반은 제어입니다. 사용자가 말을 끊을 때, 말이 끝나기 전에 답을 준비할 때, 외부 정보를 조회할 때 서버가 무엇을 결정하는지 봅니다.
"""

# ---- 2. system map -------------------------------------------------------------------------------
s = new()
s.header("01 · SYSTEM MAP", "프로세스 다섯 개와 외부 소스")
# browser
s.box(120, 270, 400, 500, g="a")
s.text(152, 296, 336, 34, "BROWSER", size=22, weight=600, color=TEAL, font=MONO, lh=1.5, g="a", extra="letter-spacing:2px;")
s.text(152, 336, 336, 46, "브라우저 (Chrome)", size=32, weight=700, lh=1.4, g="a")
for k, pair in enumerate(["마이크를 16kHz로 받아<br>0.1초마다 서버로 전송", "받은 영상과 음성을<br>오디오 시계에 맞춰 재생",
                          "설정과 대화 기록 화면<br>재생 위치를 서버에 보고"]):
    s.text(152, 404 + k * 112, 336, 76, pair, size=23, color=MUTED, lh=1.6, g="a")
# server
s.box(650, 270, 620, 500, fill=INK, border=None, g="a", delay=120)
s.text(684, 296, 552, 34, "APP SERVER · :7861 · GPU 0", size=22, weight=600, color="#F2A58E", font=MONO, lh=1.5, g="a",
       delay=120, extra="letter-spacing:2px;")
s.text(684, 336, 552, 46, "realtime_mic_app.py", size=32, weight=700, color="#F5F2EA", lh=1.4, g="a", delay=120, font=MONO)
chips = ["영상 생성 · FlashHead Lite", "말 구간 감지 · Silero VAD", "음성 인식 · Whisper turbo", "대화 제어 · 끼어들기 · 선제 응답",
         "도구함 · realtime_tools.py"]
for i, label in enumerate(chips):
    y = 410 + i * 66
    s.box(684, y, 552, 54, fill=NAVY2, border=None, radius=12, g="a", delay=200 + i * 60, dur=260)
    s.text(708, y + 11, 504, 32, label, size=22, color="#E6ECF8", lh=1.45, g="a", delay=200 + i * 60, dur=260)
s.line(524, 500, 646, 500, color=CORAL, g="a", delay=300)
s.line(646, 540, 524, 540, color=CORAL, g="a", delay=360)
s.text(120, 786, 1150, 36, "WebSocket /ws 하나로 음성·영상·제어가 모두 오간다 (원격은 SSH 터널)", size=24, color=CORAL, weight=600,
       lh=1.5, g="a", delay=420)
# local services
names = [("Supertonic TTS", ":7788", "문장을 음성으로 합성"), ("Ollama · gemma4:26b", ":11434", "답변 생성과 도구 호출"),
         ("WeKnora · Tech Trend", ":80", "지식 검색과 문서 목록")]
for i, (name, port, role) in enumerate(names):
    y = 270 + i * 170
    s.box(1400, y, 400, 150, g="b", delay=i * 100)
    s.text(1432, y + 20, 336, 32, port, size=22, weight=600, color=TEAL, font=MONO, lh=1.45, g="b", delay=i * 100)
    s.text(1432, y + 54, 336, 40, name, size=27, weight=700, lh=1.45, g="b", delay=i * 100)
    s.text(1432, y + 96, 336, 36, role, size=23, color=MUTED, lh=1.5, g="b", delay=i * 100)
    s.line(1274, y + 75, 1396, y + 75, color=INK, width=2, g="b", delay=i * 100 + 120)
# external
s.box(120, 850, 1680, 130, fill=SAND, border=None, g="c")
s.text(152, 868, 400, 34, "PUBLIC SOURCES · HTTP", size=22, weight=600, color=MUTED, font=MONO, lh=1.5, g="c",
       extra="letter-spacing:2px;")
s.text(152, 908, 1616, 44, "날씨 Open-Meteo · MET Norway &nbsp;|&nbsp; 뉴스 Google · Bing &nbsp;|&nbsp; 위키백과 &nbsp;|&nbsp; 긱뉴스",
       size=29, weight=600, lh=1.5, g="c")
s.notes = """
먼저 무엇이 떠 있는지입니다. 한 대의 서버에 프로세스가 넷, 그리고 사용자의 브라우저가 있습니다.

가운데가 앱 서버입니다. 영상 생성 모델, 말 구간 감지, 음성 인식이 이 프로세스 안에서 함께 돕니다. 브라우저와는 웹소켓 연결 하나로 음성과 영상과 제어 메시지를 모두 주고받습니다.

오른쪽 셋은 같은 서버의 다른 프로세스입니다. 음성 합성, 언어 모델, 지식베이스를 앱 서버가 필요할 때 호출합니다. 브라우저는 이들을 직접 부르지 않습니다.

아래는 인터넷의 공개 소스입니다. 날씨, 뉴스, 위키백과, 긱뉴스를 키 없이 읽기만 합니다.
"""

# ---- 3. one turn ---------------------------------------------------------------------------------
s = new()
s.header("02 · ONE TURN", "한 번의 대화가 지나가는 일곱 단계")
stages = [
    ("a", "마이크", "0.1초 단위<br>PCM 패킷", "브라우저<br>16kHz"),
    ("a", "말 끝 감지", "무음 0.7초면<br>말 끝으로 판단", "Silero VAD<br>32ms 단위"),
    ("a", "음성 인식", "한 발화를<br>통째로 글자로", "Whisper turbo<br>약 0.1초"),
    ("b", "지식 검색", "관련 문서 조각을<br>프롬프트에 첨부", "WeKnora<br>약 0.3초"),
    ("b", "답변 생성", "토큰을 흘려 받고<br>필요 시 도구 호출", "gemma4:26b<br>스트리밍"),
    ("c", "음성 합성", "문장이 끝나면<br>바로 합성", "Supertonic<br>문장 단위"),
    ("c", "영상 생성", "음성 0.96초마다<br>24프레임", "FlashHead Lite<br>0.25~0.37초"),
]
group_fill = {"a": TEAL, "b": CORAL, "c": INK}
seen = {}
for i, (g, title, body, tech) in enumerate(stages):
    x = 120 + i * 243
    d = seen.get(g, 0)
    seen[g] = d + 110
    s.box(x, 280, 220, 330, g=g, delay=d)
    s.box(x, 280, 220, 10, fill=group_fill[g], border=None, radius=0, g=g, delay=d, extra="border-radius:18px 18px 0 0;")
    s.text(x + 18, 310, 184, 40, f"0{i + 1}", size=28, weight=700, color=group_fill[g], font=MONO, lh=1.4, g=g, delay=d)
    s.text(x + 18, 356, 184, 44, title, size=29, weight=700, lh=1.45, g=g, delay=d)
    s.text(x + 18, 412, 184, 66, body, size=20, color=MUTED, lh=1.6, g=g, delay=d)
    s.text(x + 18, 524, 184, 60, tech, size=17, weight=600, color=INK, lh=1.6, g=g, delay=d)
    if i < 6:
        s.line(x + 222, 445, x + 240, 445, color=MUTED, width=2, g=stages[i + 1][0], delay=seen.get(stages[i + 1][0], 0))
labels = [("a", 120, 706, "듣기"), ("b", 849, 463, "생각하기"), ("c", 1335, 463, "말하기")]
for g, x, w, label in labels:
    s.box(x, 630, w, 6, fill=group_fill[g], border=None, radius=3, g=g, delay=200)
    s.text(x, 644, w, 36, label, size=24, weight=700, color=group_fill[g], lh=1.5, g=g, delay=200)
stats = [("2.1~2.5초", "말이 끝난 뒤 답변이 시작되기까지<br>(음성 대화, 도구 없이)"),
         ("0.96초", "영상 한 청크의 길이<br>지연이 이 박자 단위로 움직인다"),
         ("약 3.1초", "조회가 필요한 답의 시작<br>그 전에 추임새가 1.1~2.1초에 나온다")]
for i, (num, label) in enumerate(stats):
    x = 120 + i * 572
    s.box(x, 730, 536, 250, fill=SAND, border=None, g="d", delay=i * 110)
    s.text(x + 36, 756, 464, 104, num, size=72, weight=700, color=INK, lh=1.4, g="d", delay=i * 110, kind="pop", dur=280)
    s.text(x + 36, 868, 464, 80, label, size=24, color=MUTED, lh=1.6, g="d", delay=i * 110)
s.notes = """
사용자가 한마디 하고 아바타가 답하기까지, 데이터는 일곱 단계를 지납니다.

듣기입니다. 브라우저가 마이크 소리를 0.1초씩 잘라 보내면, 서버는 0.7초 동안 조용해졌을 때 말이 끝났다고 보고 그 한 덩어리를 글자로 바꿉니다.

생각하기입니다. 질문으로 지식베이스를 먼저 검색해 관련 자료를 붙이고, 언어 모델이 답을 한 토큰씩 흘려보냅니다.

말하기입니다. 답이 문장 하나만큼 모일 때마다 바로 음성으로 합성하고, 그 음성 0.96초마다 얼굴 영상 24장을 만들어 함께 보냅니다. 답 전체를 기다리지 않는 것이 핵심입니다.

측정한 지연입니다. 말이 끝나고 답이 시작되기까지 2초 남짓이고, 조회가 필요하면 추임새가 먼저 나간 뒤 3초쯤에 본 답이 시작됩니다.
"""

# ---- 4. concurrency ------------------------------------------------------------------------------
s = new()
s.header("03 · CONCURRENCY", "태스크 다섯 개가 큐로 이어져 있다")
tasks = [("receiver", "소켓에서 받은 것을 그대로 큐에 넣는다", "incoming 큐"),
         ("control", "설정, 대화 요청, 재생 위치 보고, 마이크 소리를 처리한다", "→ chat 시작·취소"),
         ("chat", "언어 모델의 답을 문장 조각으로 잘라 넘긴다", "requests 큐"),
         ("synthesizer", "조각을 하나씩 음성으로 합성해 버퍼에 쌓는다", "speech 버퍼"),
         ("main loop", "0.96초 박자로 버퍼를 잘라 영상을 만들고 보낸다", "→ 브라우저")]
for i, (name, role, out) in enumerate(tasks):
    y = 262 + i * 104
    s.box(120, y, 1080, 90, g="a", delay=i * 90, dur=300)
    s.text(150, y + 25, 250, 40, name, size=27, weight=700, color=TEAL, font=MONO, lh=1.45, g="a", delay=i * 90, dur=300)
    s.text(400, y + 26, 520, 38, role, size=23, color=INK, lh=1.6, g="a", delay=i * 90, dur=300)
    s.text(930, y + 28, 250, 34, out, size=20, weight=600, color=CORAL, font=MONO, lh=1.6, align="right", g="a",
           delay=i * 90, dur=300)
s.box(1260, 262, 540, 506, fill=INK, border=None, g="b")
s.text(1296, 286, 468, 34, "THREAD POOLS · 각 1개", size=22, weight=600, color="#F2A58E", font=MONO, lh=1.5, g="b",
       extra="letter-spacing:2px;")
pools = [("GPU 워커", "영상 생성, 기준 이미지 교체"), ("TTS 워커", "Supertonic 호출과 리샘플링"), ("STT 워커", "Whisper 음성 인식")]
for i, (name, role) in enumerate(pools):
    y = 340 + i * 104
    s.box(1296, y, 468, 90, fill=NAVY2, border=None, radius=12, g="b", delay=120 + i * 90)
    s.text(1320, y + 10, 420, 38, name, size=26, weight=700, color="#F5F2EA", lh=1.45, g="b", delay=120 + i * 90)
    s.text(1320, y + 48, 420, 32, role, size=21, color="#B9C5DE", lh=1.5, g="b", delay=120 + i * 90)
s.text(1296, 662, 468, 84, "워커가 하나뿐이라 같은 종류의 작업은<br>겹치지 않고 순서대로 처리된다", size=23, color="#E6ECF8", lh=1.6,
       g="b", delay=420)
s.box(120, 800, 1680, 180, fill=SAND, border=None, g="c")
s.text(156, 826, 1608, 44, "소켓에 쓰는 태스크가 여럿이라 잠금 하나로 순서를 지킨다", size=30, weight=700, lh=1.45, g="c")
s.text(156, 880, 1608, 76, "영상 청크, 대화 글자, 도구 진행 상황이 서로 다른 태스크에서 나온다. 세션은 동시에 하나만 받는다.<br>둘째 접속은 \"다른 세션이 사용 중\"으로 거절한다.",
       size=24, color=MUTED, lh=1.6, g="c")
s.notes = """
서버 안은 하나의 큰 루프가 아니라, 큐로 이어진 태스크 다섯 개입니다.

위에서 아래로 데이터가 흐릅니다. 받는 태스크, 요청을 해석하는 태스크, 답을 만드는 태스크, 음성을 합성하는 태스크, 그리고 박자에 맞춰 영상을 내보내는 메인 루프입니다. 각 태스크는 앞 단계를 기다리지 않고 자기 큐만 봅니다.

무거운 계산은 스레드 풀 세 개로 보냅니다. 풀마다 워커가 하나라서, 영상 생성 두 개가 그래픽 카드에서 겹치는 일이 없습니다.

여러 태스크가 한 소켓에 쓰기 때문에 잠금으로 순서를 지킵니다. 그리고 그래픽 카드가 한 사람 몫이라 세션은 한 번에 하나만 받습니다.
"""

# ---- 5. chunk clock ------------------------------------------------------------------------------
s = new()
s.header("04 · CHUNK CLOCK", "아바타는 멈추지 않는다: 0.96초 박자")
s.text(120, 256, 800, 36, "speech 버퍼 · 합성이 끝난 음성", size=24, weight=600, color=MUTED, lh=1.5, g="a")
segments = [(120, 300, TEAL, "문장 1"), (430, 60, LINE, ""), (500, 380, TEAL, "문장 2"), (890, 60, LINE, ""), (960, 240, "#7FC4C5", "문장 3 합성 중")]
for x, w, color, label in segments:
    s.box(x, 300, w, 76, fill=color, border=None, radius=10, g="a")
    if label:
        s.text(x + 12, 320, w - 24, 36, label, size=22, weight=600, color="#FFFFFF" if color == TEAL else INK, lh=1.6, align="center", g="a")
for i in range(5):
    x = 120 + i * 270
    s.box(x, 392, 3, 40, fill=CORAL, border=None, radius=0, g="b", delay=i * 70, dur=200)
    s.text(x + 10, 396, 220, 32, f"{i * 0.96:.2f}초", size=20, weight=600, color=CORAL, font=MONO, lh=1.6, g="b", delay=i * 70,
           dur=200)
s.text(1240, 256, 560, 36, "버퍼가 비면", size=24, weight=600, color=MUTED, lh=1.5, g="b", delay=200)
s.box(1240, 300, 560, 76, fill=SAND, border=LINE, radius=10, g="b", delay=200)
s.text(1260, 320, 520, 36, "무음 청크 → 가만히 듣는 얼굴", size=22, weight=600, lh=1.6, align="center", g="b", delay=200)
s.text(120, 446, 1680, 40, "메인 루프는 박자마다 버퍼 앞에서 0.96초를 잘라 영상 24장을 만들고, 음성과 한 패킷으로 묶어 보낸다.",
       size=26, lh=1.5, g="b", delay=300)
rows = [("generation", "세대 번호", "취소할 때마다 +1. 옛 번호의 합성 결과와 답변은 도착해도 버린다"),
        ("hold", "보류", "켜져 있으면 버퍼를 내보내지 않는다. 선제 응답을 숨겨 둘 때 쓴다"),
        ("kick", "즉시 전송", "끼어들기 직후, 다음 박자를 기다리지 않고 새 청크를 바로 보낸다"),
        ("marks", "시작 표시", "답변의 첫 소리가 어느 청크의 몇 초 지점인지. 지연 측정에 쓴다"),
        ("chunk_speech", "청크 장부", "청크 번호별로 어느 음성 구간이 실렸는지. 들린 데까지 자를 때 쓴다")]
head = "border:1px solid #CFC8B8;padding:10px 18px;background:#14213D;color:#F5F2EA;font-weight:700;text-align:left;"
cell = "border:1px solid #CFC8B8;padding:10px 18px;background:#FFFFFF;"
body = "".join(
    f'<tr style="height:74px"><td style="{cell}font-family:{MONO};font-weight:700;color:{TEAL};">{a}</td>'
    f'<td style="{cell}font-weight:700;">{b}</td><td style="{cell}color:{MUTED};">{c}</td></tr>' for a, b, c in rows)
s.raw(f'<div{s.anim("c")} style="position:absolute;left:120px;top:520px;width:1680px;height:440px;">'
      f'<table style="width:100%;height:100%;border-collapse:collapse;font-family:{SANS};font-size:24px;color:{INK};table-layout:fixed;">'
      f'<colgroup><col style="width:17%"><col style="width:13%"><col style="width:70%"></colgroup>'
      f'<thead><tr style="height:70px"><th style="{head}">상태</th><th style="{head}">뜻</th><th style="{head}">하는 일</th></tr></thead>'
      f'<tbody>{body}</tbody></table></div>')
s.notes = """
이 시스템의 심장은 0.96초마다 뛰는 박자입니다.

합성된 음성은 버퍼에 이어 붙습니다. 문장과 문장 사이에는 0.3초의 짧은 쉼이 들어갑니다.

메인 루프는 박자마다 버퍼 앞에서 0.96초만큼 잘라 영상을 만들고 보냅니다. 버퍼가 비어 있으면 무음을 넣습니다. 그래서 아바타는 말이 없을 때도 멈추지 않고, 듣는 표정으로 계속 움직입니다.

이 박자 위에서 흐름을 조절하는 상태가 다섯 개 있습니다. 세대 번호는 취소의 기준이고, 보류와 즉시 전송은 박자를 잠깐 바꾸는 스위치이며, 아래 둘은 무엇이 언제 재생됐는지를 적어 두는 장부입니다. 다음 두 장의 끼어들기와 선제 응답이 모두 이 다섯 개로 만들어집니다.
"""

# ---- 6. interrupt --------------------------------------------------------------------------------
s = new()
s.header("05 · BARGE-IN", "말을 끊으면: 세 가지 방식")
rows = [("basic", "서버가 아직 안 보낸 말만 버린다", "브라우저에 쌓인 음성은 계속 재생", "답변 전체가 남는다"),
        ("unmute", "브라우저도 쌓인 것을 즉시 비운다", "재생 위치를 서버에 보고", "실제 들린 데까지만 남긴다"),
        ("unmute_gate", "unmute + 짧은 추임새는 무시한다", "1.2초 넘게 말하면 끼어들기로 본다", "실제 들린 데까지만 남긴다")]
body = "".join(
    f'<tr style="height:76px"><td style="{cell}font-family:{MONO};font-weight:700;color:{TEAL};">{a}</td>'
    f'<td style="{cell}">{b}</td><td style="{cell}color:{MUTED};">{c}</td><td style="{cell}color:{MUTED};">{d}</td></tr>'
    for a, b, c, d in rows)
s.raw(f'<div{s.anim("a")} style="position:absolute;left:120px;top:256px;width:1680px;height:298px;">'
      f'<table style="width:100%;height:100%;border-collapse:collapse;font-family:{SANS};font-size:24px;color:{INK};table-layout:fixed;">'
      f'<colgroup><col style="width:14%"><col style="width:30%"><col style="width:30%"><col style="width:26%"></colgroup>'
      f'<thead><tr style="height:70px"><th style="{head}">방식</th><th style="{head}">끊는 순간</th><th style="{head}">브라우저</th>'
      f'<th style="{head}">대화 이력</th></tr></thead><tbody>{body}</tbody></table></div>')
steps = [("서버", "세대 번호 +1<br>버퍼와 큐를 비우고<br>답변 생성을 취소"), ("서버 → 브라우저", "interrupt 메시지<br>\"이 번호 앞 청크는<br>모두 버려라\""),
         ("브라우저 → 서버", "0.03초 페이드 후 비움<br>어느 청크의 몇 초까지<br>재생했는지 보고"), ("서버", "청크 장부로 환산해<br>이력의 답변을<br>들린 문장까지로 자름")]
for i, (who, what) in enumerate(steps):
    x = 120 + i * 315
    s.box(x, 592, 290, 240, g="b", delay=i * 130)
    s.text(x + 24, 614, 242, 40, f"0{i + 1}", size=28, weight=700, color=CORAL, font=MONO, lh=1.4, g="b", delay=i * 130)
    s.text(x + 24, 654, 242, 34, who, size=21, weight=700, color=TEAL, lh=1.6, g="b", delay=i * 130)
    s.text(x + 24, 696, 242, 112, what, size=22, color=INK, lh=1.65, g="b", delay=i * 130)
    if i < 3:
        s.line(x + 292, 712, x + 312, 712, color=MUTED, width=2, g="b", delay=i * 130 + 100)
s.box(1400, 592, 400, 240, fill=INK, border=None, g="c")
s.text(1432, 612, 336, 34, "끊긴 뒤 남는 소리", size=23, weight=600, color="#B9C5DE", lh=1.5, g="c")
s.text(1432, 652, 336, 96, "0.62초 → 0.02초", size=42, weight=700, color="#F5F2EA", lh=2.2, g="c", kind="pop", dur=280)
s.text(1432, 766, 336, 32, "basic → unmute, 실측", size=21, color="#F2A58E", lh=1.5, g="c")
s.text(120, 866, 1680, 84, "이력을 자르는 이유: 아바타가 실제로 하지 못한 말을 \"이미 말했다\"고 기억하면 다음 답이 어긋난다.<br>아무것도 들리지 않았다면 그 답과 그에 딸린 조회 기록까지 이력에서 뺀다.",
       size=25, color=MUTED, lh=1.65, g="c", delay=150)
s.notes = """
아바타가 말하는 도중에 사용자가 말을 시작하면 어떻게 할지입니다. 화면에서 세 가지 중 고를 수 있습니다.

기본 방식은 서버만 멈춥니다. 브라우저에 이미 가 있는 음성은 끝까지 나옵니다. 언뮤트 방식은 브라우저까지 비우고, 게이트 방식은 여기에 더해 네, 그렇군요 같은 짧은 추임새에는 멈추지 않습니다.

언뮤트 방식의 순서입니다. 서버가 세대 번호를 올려 진행 중인 것을 무효로 만들고, 브라우저에 버리라고 알립니다. 브라우저는 어디까지 재생했는지 돌려주고, 서버는 그 위치를 문장으로 환산해 대화 이력을 자릅니다.

효과는 끊긴 뒤 남는 소리로 쟀습니다. 0.62초에서 0.02초로 줄었습니다. 이력을 자르는 것은 아바타가 하지 못한 말을 한 것으로 기억하지 않게 하려는 것입니다.
"""

# ---- 7. speculative ------------------------------------------------------------------------------
s = new()
s.header("06 · SPECULATIVE REPLY", "말이 끝났다고 확신하기 전에 답을 준비한다")
s.box(160, 398, 1600, 6, fill=INK, border=None, radius=3, g="a")
points = [(160, "0초", "마지막 소리", INK), (560, "0.2초", "잠깐 멈춤 감지", CORAL), (1360, "0.7초", "말 끝 확정", TEAL)]
for x, t, label, color in points:
    s.box(x - 13, 388, 26, 26, fill=color, border=None, radius=13, g="a")
    s.text(x - 130, 318, 260, 58, t, size=38, weight=700, color=color, font=MONO, lh=1.5, align="center", g="a")
    s.text(x - 150, 424, 300, 36, label, size=24, weight=600, lh=1.5, align="center", g="a")
s.box(560, 480, 800, 104, fill="#FBE3DB", border=None, radius=14, g="a", delay=200)
s.text(590, 494, 740, 76, "이 0.5초 동안 인식 → 지식 검색 → 답변 → 합성을 먼저 돌린다<br>hold가 켜져 있어 아무 소리도 나가지 않는다",
       size=23, lh=1.65, align="center", g="a", delay=200)
s.box(120, 626, 820, 220, g="b")
s.text(156, 646, 748, 36, "COMMIT", size=22, weight=600, color=TEAL, font=MONO, lh=1.5, g="b", extra="letter-spacing:2px;")
s.text(156, 684, 748, 44, "0.7초까지 조용했다", size=30, weight=700, lh=1.45, g="b")
s.text(156, 738, 748, 84, "모아 둔 메시지를 순서대로 내보내고 hold를 끈다.<br>준비한 답이 다음 박자에 바로 실린다.", size=24, color=MUTED, lh=1.7,
       g="b")
s.box(980, 626, 820, 220, g="b", delay=140)
s.text(1016, 646, 748, 36, "DISCARD", size=22, weight=600, color=CORAL, font=MONO, lh=1.5, g="b", delay=140,
       extra="letter-spacing:2px;")
s.text(1016, 684, 748, 44, "사용자가 말을 이어 갔다", size=30, weight=700, lh=1.45, g="b", delay=140)
s.text(1016, 738, 748, 84, "준비하던 답을 취소하고 이력에서도 지운다.<br>전체 문장이 끝나면 처음부터 다시 답한다.", size=24, color=MUTED, lh=1.7,
       g="b", delay=140)
s.box(120, 880, 1680, 100, fill=SAND, border=None, g="c")
s.text(156, 894, 1608, 72, "측정: 평균 2.5~2.8초 → 2.3초. 이득은 조금씩이 아니라 영상 한 박자(0.96초) 단위로 생긴다.<br>인식한 글이 문장으로 끝나지 않으면 준비 자체를 하지 않는다.",
       size=24, lh=1.45, g="c")
s.notes = """
지연을 줄이려는 장치입니다. 말이 끝났다고 판단하려면 0.7초의 침묵을 기다려야 하는데, 그 시간을 그냥 보내지 않습니다.

0.2초만 조용해도 일단 답을 준비하기 시작합니다. 다만 보류 스위치를 켜 두어서, 만든 음성이 버퍼에 쌓이기만 하고 나가지는 않습니다.

그다음은 둘 중 하나입니다. 0.7초까지 조용하면 준비한 답을 그대로 내보냅니다. 사용자가 다시 말하기 시작하면 준비한 것을 버리고, 이력에서도 흔적을 지웁니다.

효과는 평균 0.2초에서 0.5초 정도입니다. 줄어드는 방식이 특이한데, 미리 준비한 덕에 한 박자 빠른 청크에 실릴 때만 0.96초가 통째로 당겨집니다.
"""

# ---- 8. reply logic ------------------------------------------------------------------------------
s = new()
s.header("07 · REPLY LOGIC", "답 하나를 만드는 다섯 걸음")
flow = [("a", "턴 설정", "언어는 고정하거나 방금 쓴 말로 판별한다.<br>길이는 설정값, \"자세히\"·\"짧게\"라고 하면 그 턴만 바뀐다"),
        ("a", "지식 검색", "점수가 기준(0.4) 이상인 조각만 붙인다.<br>\"그거\", \"두 번째\"처럼 앞을 가리키는 말이면 직전 자료를 다시 쓴다"),
        ("b", "프롬프트 조립", "시스템 지시 + 오늘 날짜와 달력 + 도구 안내 + 길이 지시.<br>지금 시각은 사용자 말 바로 앞에 붙인다"),
        ("b", "생성과 도구", "도구를 부르면 추임새를 먼저 말하고, 결과를 받아 다시 생성한다.<br>조회는 한 답에 두 번까지"),
        ("b", "문장 조각", "첫 조각은 15자, 그다음은 40자가 넘는 문장 끝에서 잘라<br>합성 큐로 보낸다")]
delays = {}
for i, (g, title, text) in enumerate(flow):
    y = 256 + i * 146
    d = delays.get(g, 0)
    delays[g] = d + 110
    s.box(120, y, 1040, 130, g=g, delay=d, dur=300)
    s.text(148, y + 38, 70, 54, f"0{i + 1}", size=36, weight=700, color=CORAL, font=MONO, lh=1.5, g=g, delay=d, dur=300)
    s.text(228, y + 12, 300, 42, title, size=28, weight=700, lh=1.5, g=g, delay=d, dur=300)
    s.text(228, y + 54, 900, 60, text, size=20, color=MUTED, lh=1.5, g=g, delay=d, dur=300)
s.box(1220, 256, 580, 714, fill=INK, border=None, g="c")
s.text(1256, 280, 508, 34, "BEFORE SPEAKING", size=22, weight=600, color="#F2A58E", font=MONO, lh=1.5, g="c",
       extra="letter-spacing:2px;")
s.text(1256, 320, 508, 96, "소리 내기 전에<br>문장을 검사한다", size=34, weight=700, color="#F5F2EA", lh=1.4, g="c")
checks = [("요일과 날짜가 달력과 다르다", "\"토요일은 21일\""), ("조회 없이 조회한 척한다", "\"확인해 보니 비가…\""), ("조회 뒤에 아무 말도 없다", "빈 답변")]
for i, (what, example) in enumerate(checks):
    y = 444 + i * 118
    s.box(1256, y, 508, 102, fill=NAVY2, border=None, radius=12, g="c", delay=120 + i * 100)
    s.text(1280, y + 12, 460, 38, what, size=24, weight=700, color="#F5F2EA", lh=1.55, g="c", delay=120 + i * 100)
    s.text(1280, y + 54, 460, 34, example, size=21, color="#B9C5DE", lh=1.6, g="c", delay=120 + i * 100)
s.text(1256, 812, 508, 132, "걸리면 화면의 글을 거두고 고쳐서<br>한 번 더 생성한다. 또 걸리면<br>그 문장만 빼고 말한다.", size=23, color="#E6ECF8", lh=1.75,
       g="c", delay=440)
s.notes = """
이제 답 하나가 만들어지는 과정을 봅니다. 대화 태스크가 하는 일입니다.

먼저 이번 턴의 언어와 길이를 정하고, 질문으로 지식베이스를 검색합니다. 관련도가 기준을 넘는 자료만 붙입니다. 그거 누가 만들었어 같은 후속 질문은 그 문장으로 검색하면 엉뚱한 문서가 나오므로, 직전 턴의 자료를 이어 씁니다.

프롬프트를 조립하고 생성을 시작합니다. 모델이 도구를 부르면 확인해 볼게요 같은 추임새를 먼저 말해 침묵을 메우고, 결과를 받아 다시 생성합니다. 나온 글은 문장 끝에서 잘라 바로 합성으로 넘깁니다.

마지막 관문입니다. 문장을 소리 내기 전에 세 가지를 검사합니다. 날짜가 틀렸거나, 조회하지 않고 조회한 것처럼 말하거나, 아무 말도 안 하면 한 번 다시 시킵니다. 글은 화면에 먼저 보이지만 음성은 아직 나가지 않았기 때문에 거둘 수 있습니다.
"""

# ---- 9. tools ------------------------------------------------------------------------------------
s = new()
s.header("08 · LOOKUPS", "모델이 고르는 도구 일곱 개와 지식 검색")
rows = [("get_weather", "날씨", "Open-Meteo와 MET Norway에 동시 요청, 먼저 온 답 사용", "0.78초"),
        ("search_news", "뉴스 제목", "Google 뉴스. 거절되면 재시도, 그래도 안 되면 Bing", "0.07~0.8초"),
        ("get_news_detail", "뉴스 하나 자세히", "Bing 뉴스에서 같은 사건 기사 4건의 앞부분", "0.3~2.8초"),
        ("search_geeknews", "기술 소식", "긱뉴스 목록 약 140건을 10분간 보관하며 검색", "0.5~1.3초"),
        ("read_geeknews", "긱뉴스 글 하나", "글 페이지의 한국어 요약 전체", "0.06~1.3초"),
        ("search_wikipedia", "백과 지식", "한국어판 먼저, 없으면 영어판. 무관한 문서는 버림", "0.4~0.8초"),
        ("list_knowledge_by_date", "날짜별 자료", "WeKnora 문서 목록을 등록일로 거른다", "0.01~0.02초"),
        ("지식 검색 (도구 아님)", "모든 질문 앞에", "WeKnora 검색과 리랭크. 점수가 기준 이상일 때만 사용", "0.26~0.43초")]
body = "".join(
    f'<tr style="height:72px"><td style="{cell}font-family:{MONO};font-weight:700;color:{TEAL if i < 7 else CORAL};font-size:21px;">{a}</td>'
    f'<td style="{cell}font-weight:700;">{b}</td><td style="{cell}color:{MUTED};">{c}</td>'
    f'<td style="{cell}font-family:{MONO};font-size:21px;">{d}</td></tr>' for i, (a, b, c, d) in enumerate(rows))
s.raw(f'<div{s.anim("a")} style="position:absolute;left:120px;top:252px;width:1680px;height:644px;">'
      f'<table style="width:100%;height:100%;border-collapse:collapse;font-family:{SANS};font-size:23px;color:{INK};table-layout:fixed;">'
      f'<colgroup><col style="width:23%"><col style="width:15%"><col style="width:48%"><col style="width:14%"></colgroup>'
      f'<thead><tr style="height:68px"><th style="{head}">이름</th><th style="{head}">쓰는 때</th><th style="{head}">출처와 동작</th>'
      f'<th style="{head}">측정 시간</th></tr></thead><tbody>{body}</tbody></table></div>')
notes3 = [("읽기 전용", "모두 조회만 한다. 그래서 선제 응답 중에 먼저 실행해도 된다"), ("제한 시간 4초", "넘으면 포기하고 \"지금은 확인이 어렵다\"고 답한다"),
          ("취소 가능", "말을 끊으면 진행 중인 조회도 함께 취소된다")]
for i, (k, v) in enumerate(notes3):
    x = 120 + i * 572
    s.box(x, 916, 536, 84, fill=SAND, border=None, radius=14, g="b", delay=i * 100)
    s.text(x + 24, 926, 488, 30, k, size=21, weight=700, color=CORAL, lh=1.4, g="b", delay=i * 100)
    s.text(x + 24, 958, 488, 30, v, size=19, color=INK, lh=1.5, g="b", delay=i * 100)
s.notes = """
아바타가 모르는 것을 찾아오는 통로들입니다.

위의 일곱 개는 언어 모델이 질문을 듣고 스스로 고르는 도구입니다. 날씨, 뉴스 제목과 상세, 긱뉴스 검색과 글 읽기, 위키백과, 그리고 지식베이스에 날짜별로 무엇이 들어왔는지입니다. 맨 아래 지식 검색은 성격이 다릅니다. 모델이 고르는 것이 아니라 모든 질문 앞에서 항상 돌고, 관련도가 높을 때만 결과를 씁니다.

공통 규칙이 셋 있습니다. 모두 읽기만 하므로 답을 미리 준비하는 중에 실행해도 부작용이 없습니다. 4초 안에 안 오면 포기합니다. 그리고 사용자가 말을 끊으면 조회도 같이 취소됩니다.
"""

# ---- 10. limits ----------------------------------------------------------------------------------
s = new(bg=INK)
s.text(120, 84, 900, 34, "09 · STATUS", size=22, weight=600, color="#F2A58E", font=MONO, lh=1.5, g="autoh",
       extra="letter-spacing:2px;")
s.text(120, 124, 1680, 84, "확인한 범위와 남은 한계", size=58, weight=700, color="#F5F2EA", lh=1.4, g="autoh", delay=80, dur=520)
s.box(120, 220, 1680, 3, fill="#3A4A72", border=None, radius=0)
s.box(120, 270, 800, 520, fill=NAVY2, border=None, g="a")
s.text(156, 296, 728, 34, "VERIFIED", size=22, weight=600, color="#7FD6D7", font=MONO, lh=1.5, g="a", extra="letter-spacing:2px;")
s.text(156, 336, 728, 48, "이렇게 확인했다", size=34, weight=700, color="#F5F2EA", lh=1.4, g="a")
done = ["웹소켓 모의 클라이언트로 텍스트·음성 경로를 실행", "도구는 하나씩 직접 호출해 결과와 시간을 측정", "날짜별 자료 건수는 WeKnora DB와 대조",
        "지연과 끼어들기 수치는 모두 실측값"]
for i, line in enumerate(done):
    y = 416 + i * 84
    s.box(156, y + 12, 14, 14, fill="#7FD6D7", border=None, radius=7, g="a", delay=120 + i * 90)
    s.text(188, y, 696, 40, line, size=24, color="#E6ECF8", lh=1.6, g="a", delay=120 + i * 90)
s.box(1000, 270, 800, 520, fill=NAVY2, border=None, g="b")
s.text(1036, 296, 728, 34, "OPEN", size=22, weight=600, color="#F2A58E", font=MONO, lh=1.5, g="b", extra="letter-spacing:2px;")
s.text(1036, 336, 728, 48, "아직 남아 있다", size=34, weight=700, color="#F5F2EA", lh=1.4, g="b")
todo = ["스피커로 들으면 아바타 음성이 마이크로 되돌아온다", "말 끝 판단이 고정 0.7초: 문장 사이 쉼에 끊길 수 있다", "영상 한 박자 0.96초가 지연의 바닥이다",
        "동시 접속 한 명, 접속 인증 없음"]
for i, line in enumerate(todo):
    y = 416 + i * 84
    s.box(1036, y + 12, 14, 14, fill="#F2A58E", border=None, radius=7, g="b", delay=120 + i * 90)
    s.text(1068, y, 696, 40, line, size=24, color="#E6ECF8", lh=1.6, g="b", delay=120 + i * 90)
s.box(120, 830, 1680, 150, fill=CORAL, border=None, g="c")
s.text(156, 852, 1608, 48, "다음 후보: 말 끝을 예측하는 모델로 고정 0.7초 대기를 바꾸기", size=34, weight=700, color="#FFFFFF", lh=1.4, g="c")
s.text(156, 908, 1608, 44, "끝난 문장에는 더 빨리 답하고, 문장 사이의 쉼에는 기다리게 된다. 한국어 정확도는 붙여서 재 봐야 한다.", size=25,
       color="#FFE9E2", lh=1.6, g="c")
s.notes = """
마지막으로, 지금 상태를 어디까지 믿을 수 있는지 말씀드립니다.

확인한 방법입니다. 브라우저 대신 웹소켓 클라이언트를 만들어 텍스트와 음성 경로를 돌렸고, 도구는 하나씩 직접 불러 결과를 봤습니다. 앞에서 보여 드린 숫자는 모두 그렇게 잰 값입니다.

남은 한계입니다. 스피커로 들으면 아바타 목소리가 마이크로 다시 들어가므로 헤드폰이 필요합니다. 말 끝을 고정된 0.7초로 판단해서 문장 사이에 길게 쉬면 중간에 답을 시작합니다. 그리고 영상 한 박자가 지연의 바닥입니다.

다음에 손댈 곳은 말 끝 판단입니다. 고정 대기 대신 예측 모델을 쓰면 지연과 중간 끊김이 함께 나아질 수 있습니다. 다만 한국어에서 얼마나 정확한지는 붙여서 측정해 봐야 합니다.
"""

HEAD = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>실시간 대화 아바타 아키텍처</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ width: 1920px; height: 1080px; overflow: hidden; position: relative; background: #0b1224; }}
.slide {{ position: absolute; inset: 0; display: none; overflow: hidden; background: #fff; }}
.slide.active {{ display: block; }}
</style>
</head>
<body>

"""
out = HEAD + "\n\n".join(s.html(i == 0) for i, s in enumerate(slides)) + "\n\n</body>\n</html>\n"
open(sys.argv[1], "w", encoding="utf-8").write(out)
print(len(slides), "slides,", len(out), "bytes")
