/* Shared by the three v2 screens: API access with designed failure states, small DOM helpers,
   the labels people read. No framework, no build step. */
'use strict';

const $ = id => document.getElementById(id);

/** el('div', {class: 'x', onclick: fn, hidden: true}, 'text', childNode, [more]) - text is never parsed as HTML. */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') node.style.cssText = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  node.append(...kids(children));
  return node;
}

const kids = list => list.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false);
/** replaceChildren that takes nested lists and skips null, like el(). */
function setKids(node, ...children) { node.replaceChildren(...kids(children)); return node; }

class ApiError extends Error {
  /** kind: unreachable (no server), missing (the route does not exist: module not installed),
      notfound (the route exists, the thing does not), refused (409, a rule said no), http (anything else) */
  constructor(kind, status, message) { super(message); this.kind = kind; this.status = status; }
}

function reasonOf(body, fallback) {
  if (!body) return fallback;
  if (typeof body === 'string') return body.slice(0, 300) || fallback;
  const d = body.detail !== undefined ? body.detail : (body.message || body.error || body.reason);
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => x.msg || JSON.stringify(x)).join(', ');
  if (d && typeof d === 'object') return d.message || d.reason || d.error || JSON.stringify(d);
  return fallback;
}

/** Calls the v2 server. Returns parsed JSON; throws ApiError. */
async function api(path, options) {
  const opts = Object.assign({ method: 'GET' }, options || {});
  const init = { method: opts.method, headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  const abort = new AbortController();
  init.signal = abort.signal;
  const timer = setTimeout(() => abort.abort(), opts.timeout || 20000);
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new ApiError('unreachable', 0, '서버에 연결할 수 없습니다');
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401 && location.pathname !== '/login') {
    // the owner login ran out (agentkit/auth.py): log in again and come back here
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new ApiError('login', 401, '로그인이 필요합니다');
  }
  let body = null;
  const text = await res.text().catch(() => '');
  if (text) { try { body = JSON.parse(text); } catch (err) { body = text; } }
  if (res.ok) return body;
  if (res.status === 404) {
    // FastAPI answers {"detail": "Not Found"} when no route matches: the module that owns it is not installed.
    const routeMissing = !body || typeof body === 'string' || body.detail === 'Not Found';
    throw new ApiError(routeMissing ? 'missing' : 'notfound', 404, reasonOf(body, '찾을 수 없습니다'));
  }
  if (res.status === 405) throw new ApiError('missing', 405, '이 기능은 아직 설치되지 않았습니다');
  if (res.status === 503) throw new ApiError('missing', 503, reasonOf(body, '필요한 모듈을 쓸 수 없습니다'));
  if (res.status === 409) throw new ApiError('refused', 409, reasonOf(body, '서버가 요청을 거절했습니다'));
  throw new ApiError('http', res.status, reasonOf(body, '서버 오류 (' + res.status + ')'));
}

const asList = (value, ...keys) => {
  if (Array.isArray(value)) return value;
  for (const key of keys) if (value && Array.isArray(value[key])) return value[key];
  return [];
};

/** Replaces the content of `target` with a designed state block.
    kind: loading | empty | unreachable | missing | error. */
function stateView(target, kind, info) {
  info = info || {};
  let box;
  if (kind === 'loading') {
    box = el('div', { class: 'stateview loading', role: 'status' }, info.title || '불러오는 중입니다');
  } else if (kind === 'unreachable') {
    box = el('div', { class: 'stateview bad', role: 'alert' },
      el('strong', {}, '서버에 연결할 수 없습니다'),
      el('span', {}, '이 화면은 v2 서버에서 자료를 받아 옵니다. 아래를 확인한 뒤 다시 시도하세요.'),
      el('ol', {},
        el('li', {}, '서버가 켜져 있는지 확인합니다. v2 폴더에서 ', el('code', {}, 'python -m agentkit.server --port 7871')),
        el('li', {}, '주소창의 주소와 포트가 서버와 같은지 확인합니다. 지금 주소: ', el('code', {}, location.host || '(없음)'))),
      info.retry ? el('button', { class: 'ghost small', onclick: info.retry }, '다시 연결') : null);
  } else if (kind === 'missing') {
    box = el('div', { class: 'stateview' },
      el('strong', {}, info.title || '이 기능은 아직 쓸 수 없습니다'),
      el('span', {}, info.hint || '서버에 이 기능을 맡은 모듈이 설치되지 않았습니다. 나머지 화면은 그대로 쓸 수 있습니다.'));
  } else if (kind === 'error') {
    box = el('div', { class: 'stateview bad', role: 'alert' },
      el('strong', {}, info.title || '불러오지 못했습니다'),
      el('span', {}, info.hint || ''),
      info.retry ? el('button', { class: 'ghost small', onclick: info.retry }, '다시 시도') : null);
  } else {
    box = el('div', { class: 'stateview' },
      el('strong', {}, info.title || '아직 내용이 없습니다'),
      info.hint ? el('span', {}, info.hint) : null,
      info.action || null);
  }
  if (info.center) box.classList.add('center');
  target.replaceChildren(box);
  return box;
}

/** Shows the state that fits an ApiError. `what` names the feature for the "not installed" case. */
function errorView(target, err, what, retry) {
  if (!(err instanceof ApiError)) { console.error(err); return stateView(target, 'error', { title: '화면 오류', hint: String(err && err.message || err), retry }); }
  if (err.kind === 'unreachable') return stateView(target, 'unreachable', { retry });
  if (err.kind === 'missing') return stateView(target, 'missing', { title: what ? what + ' 기능은 아직 쓸 수 없습니다' : undefined });
  return stateView(target, 'error', { title: (what || '자료') + '을(를) 불러오지 못했습니다', hint: err.message, retry });
}

// ---- words -------------------------------------------------------------------------------------
const CHANNEL_LABEL = { text: '글', voice: '음성', avatar: '아바타' };
const STATUS_LABEL = { active: '진행 중', completed: '끝남', handoff: '상담원 연결', abandoned: '중단됨', error: '오류' };
const STATUS_CLASS = { active: 'b-live', completed: '', handoff: 'b-out', abandoned: '', error: 'b-safety' };
const STATE_LABEL = { draft: '초안', tested: '시험 완료', pending_approval: '승인 대기', approved: '승인됨', deployed: '배포됨', retired: '물러남' };
const STATE_CLASS = { draft: '', tested: 'b-out', pending_approval: 'b-out', approved: 'b-back', deployed: 'b-solid', retired: '' };
const KIND_LABEL = { endpoint: '말 끝 판단', stt: '음성 인식', retrieval: '지식 검색', llm: '답변 생성', tool: '도구 호출',
  guard: '말하기 전 검사', tts: '음성 합성', output: '첫 출력', interrupt: '끼어들기', handoff: '상담원 연결',
  display: '화면 표시', action: '화면 터치' };
const KINDS = Object.keys(KIND_LABEL);
const ERROR_LABEL = { not_found: '찾지 못함', permission_denied: '권한 없음', invalid_input: '입력 오류',
  precondition_failed: '먼저 필요한 단계가 빠짐', not_confirmed: '동의 없음', system_error: '시스템 장애', timeout: '시간 초과' };
const FIELD_LABEL = { persona: '역할과 말투', reply_length: '답변 길이', language: '언어', tools: '도구', flow: '흐름 규칙',
  handoff: '넘김 조건', prohibitions: '금지 사항', guards: '검사 규칙', evaluation: '평가', tests: '시험', knowledge: '지식',
  voice: '음성 설정', avatar: '아바타', llm: '모델', name: '이름', description: '설명', live_clock: '날짜·시각 안내' };
const SAFETY_FIELDS = ['tools', 'handoff', 'prohibitions', 'guards'];
const METRIC_LABEL = { response_ms_median: '응답 지연 중앙값', response_ms_max: '가장 느린 응답', tool_failures: '도구 실패',
  interruptions: '끼어들기', turns: '턴 수', handoff: '상담원 연결', pass_rate: '통과율', safety_failures: '안전 기준 실패',
  tool_failure_rate: '도구 실패율', handoff_rate: '넘김 비율' };

const sec = ms => (ms === null || ms === undefined || isNaN(ms)) ? '-' : (ms / 1000).toFixed(2) + '초';
const pct = x => (x === null || x === undefined || isNaN(x)) ? '-' : Math.round(x * 100) + '%';
const pad2 = n => String(n).padStart(2, '0');
function clock(epoch) {
  if (!epoch) return '-';
  const d = new Date(epoch * 1000), now = new Date();
  const time = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  return d.toDateString() === now.toDateString() ? '오늘 ' + time : (d.getMonth() + 1) + '월 ' + d.getDate() + '일 ' + time;
}
function metricText(name, value) {
  if (value === null || value === undefined) return '-';
  if (/_ms/.test(name)) return sec(value);
  if (/rate$/.test(name)) return pct(value);
  if (name === 'handoff') return value ? '예' : '아니오';
  return String(Math.round(value * 100) / 100);
}
const pretty = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const badge = (text, cls, title) => el('span', { class: 'badge ' + (cls || ''), title }, text);

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== '' && v !== null && v !== undefined) p.set(k, v);
  const s = p.toString();
  return s ? '?' + s : '';
}

/** Agents as {id, name, description, deployment}. The server may send bare ids or objects. */
async function loadAgents() {
  const raw = asList(await api('/api/agents'), 'agents');
  return raw.map(a => typeof a === 'string' ? { id: a, name: a, description: '', deployment: null }
    : { id: a.id || a.agent_id, name: a.name || a.id || a.agent_id, description: a.description || '',
        deployment: a.deployment || null, raw: a });
}

/** The Set `registered` expects: the id of every contract the server has ("cafe.search_menu", or
 *  the bare name of a contract that is in no pack). tools: the rows of GET /api/tools. */
function toolIds(tools) {
  return new Set((tools || []).map(t => t.id || t.name));
}

/** Whether an agent's tool name has a contract: a bare name is looked up in the agent's tool packs
 *  (the cafe and the burger shop both have a "search_menu"); without packs, in any pack. */
function toolRegistered(name, packs, registered) {
  if (registered.has(name)) return true;
  if (packs.length) return packs.some(pack => registered.has(pack + '.' + name));
  for (const id of registered) if (id.endsWith('.' + name)) return true;
  return false;
}

/** The newest version's lifecycle state, or '' when the row does not say. */
function newestState(agent) {
  const versions = agent.versions || (agent.raw && agent.raw.versions) || [];
  if (!versions.length) return '';
  return versions.reduce((a, b) => (b.version > a.version ? b : a)).state || '';
}

/** How far along an agent is, in words for the selector: deployed and usable, deployed with tools
 *  missing, not deployed yet (with how far its newest version got: draft, tested, waiting for
 *  approval, approved), or only an outline whose tools nobody has registered yet (cannot hold a
 *  conversation). registered: Set from toolIds(), or null when unknown. */
function agentReadiness(agent, registered) {
  const stable = agent.stable || (agent.deployment && agent.deployment.stable);
  const tools = agent.tools || (agent.raw && agent.raw.tools) || [];   // loadAgents keeps the server row in .raw
  const packs = agent.tool_packs || (agent.raw && agent.raw.tool_packs) || [];
  const missing = registered ? tools.filter(name => !toolRegistered(name, packs, registered)) : [];
  if (agent.error || (agent.raw && agent.raw.error)) return { key: 'broken', label: '정의서 오류', usable: false };
  if (missing.length && !stable) return { key: 'outline', label: '뼈대 · 도구 ' + missing.length + '개 미등록, 대화 불가', usable: false, missing };
  if (missing.length) return { key: 'partial', label: '운영 중 v' + stable + ' · 도구 ' + missing.length + '개 미등록', usable: true, missing };
  if (stable) return { key: 'live', label: '운영 중 v' + stable, usable: true, missing };
  const state = newestState(agent) || 'draft';
  return { key: 'draft', state, label: (STATE_LABEL[state] || STATE_LABEL.draft) + ' · 배포 전', usable: true, missing };
}

/** registered (optional): Set of registered tool names; with it each option says how ready the agent is,
 *  and usable agents come first. */
function fillAgentSelect(select, agents, chosen, withAll, registered) {
  select.replaceChildren();
  if (withAll) select.add(new Option('모든 에이전트', ''));
  const rows = agents.map(a => ({ a, ready: registered === undefined ? null : agentReadiness(a, registered) }));
  if (registered !== undefined) {
    const rank = { live: 0, partial: 1, draft: 2, outline: 3, broken: 4 };
    rows.sort((x, y) => rank[x.ready.key] - rank[y.ready.key]);
  }
  rows.forEach(({ a, ready }) => {
    const name = a.name === a.id ? a.id : a.name + ' (' + a.id + ')';
    const option = new Option(ready ? name + ' — ' + ready.label : name, a.id, false, a.id === chosen);
    if (ready) option.dataset.readiness = ready.key;
    select.add(option);
  });
}

/** The top bar's page links. `here`: talk | monitor | studio. */
function mountNav(here) {
  const nav = $('nav');
  if (!nav) return;
  [['talk', '/', '대화'], ['monitor', '/monitor', '모니터'], ['studio', '/studio', '제작실'], ['connect', '/connect', '계정 연결']].forEach(([key, href, label]) => {
    nav.append(el('a', { href, 'aria-current': key === here ? 'page' : null }, label));
  });
}

// ---- speech recognition quality (docs/ASR_QUALITY.md) -------------------------------------------
const FLAG_LABEL = { low_confidence: '낮은 신뢰도', repeat: '같은 말 반복', reask: '되물음', complaint: '불만 표현', empty_wait: '기다리라고만 함' };
const FLAG_HINT = { low_confidence: '음성 인식이 스스로 불확실하다고 본 턴입니다', repeat: '고객이 바로 앞에서 한 말을 다시 했습니다',
  reask: '에이전트가 다시 말해 달라고 했습니다', complaint: '고객이 이미 말했다거나 오래 기다린다고 했습니다',
  empty_wait: '에이전트가 기다려 달라고만 하고 아무 일도 하지 않았습니다' };
const ENGINE_LABEL = { whisper: 'Whisper', qwen: 'Qwen3-ASR' };
const flagsOf = turn => Object.assign({ auto: [], misheard: null }, turn && turn.flags);

/** `got` as nodes, compared letter by letter with `expected` (spaces and punctuation are not compared, as in the
    character error rate): letters that differ are marked, letters that are missing are shown struck through. */
function charDiff(expected, got) {
  const counted = ch => /[\p{L}\p{N}]/u.test(ch);
  const raw = [...String(got || '')];
  const a = [...String(expected || '')].filter(counted).map(c => c.toLowerCase());
  if (!a.length) return [raw.join('')];
  const b = raw.filter(counted).map(c => c.toLowerCase());
  const n = a.length, m = b.length;
  const t = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) t[i][j] = a[i] === b[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
  const same = new Array(m).fill(false), missing = Array.from({ length: m + 1 }, () => '');
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { same[j] = true; i++; j++; }
    else if (t[i + 1][j] >= t[i][j + 1]) { missing[j] += a[i]; i++; }
    else j++;
  }
  while (i < n) missing[m] += a[i++];
  const parts = [];   // [kind, text], neighbours of one kind merged
  const push = (kind, text) => { const last = parts[parts.length - 1]; if (last && last[0] === kind) last[1] += text; else parts.push([kind, text]); };
  let k = 0;
  raw.forEach(ch => {
    if (!counted(ch)) { push('', ch); return; }
    if (missing[k]) push('miss', missing[k]);
    push(same[k] ? '' : 'wrong', ch);
    k++;
  });
  if (missing[m]) push('miss', missing[m]);
  return parts.map(([kind, text]) => kind === 'wrong' ? el('mark', { class: 'd-wrong', title: '다르게 들은 글자' }, text)
    : kind === 'miss' ? el('del', { class: 'd-miss', title: '빠진 글자' }, text) : text);
}

/** The "잘못 알아들음" action of one turn, with the optional one-line field for what was actually said.
    opts: sid, turn (number or function returning it), said (the utterance, for the accessible name), flags,
    recorded (false: say that no audio is kept),
    onChange(flags, case). Never moves the keyboard focus by itself. Returns {node, retry()}. */
function mishearControl(opts) {
  let flags = Object.assign({ auto: [], misheard: null }, opts.flags), editing = false, busy = false, problem = null, pending = null, saved = false;
  const about = opts.said ? '고객의 말 “' + String(opts.said).slice(0, 60) + '”' : '이 고객의 말';
  // a group of controls about one utterance, not a line of the conversation: it is not announced with the chat
  const node = el('div', { class: 'mh', role: 'group', 'aria-live': 'off', 'aria-label': about + '에 대한 표시' });
  const turnNo = () => typeof opts.turn === 'function' ? opts.turn() : opts.turn;
  const input = el('input', { type: 'text', placeholder: '실제로 한 말 (적지 않아도 됩니다)', 'aria-label': '실제로 한 말', maxlength: '300' });

  async function post(body) {
    busy = true; problem = null; pending = body; paint();
    try {
      const res = await api('/api/sessions/' + encodeURIComponent(opts.sid) + '/turns/' + turnNo() + '/flag', { method: 'POST', body });
      flags = Object.assign({ auto: flags.auto || [] }, (res && res.flags) || body);
      pending = null;
      if (opts.onChange) opts.onChange(flags, res && res.case);
      return true;
    } catch (err) {
      problem = err;
      return false;
    } finally { busy = false; paint(); }
  }
  const mark = () => { flags.misheard = true; editing = true; saved = false; input.value = flags.actual_text || ''; post({ misheard: true }); };
  const unmark = async () => { const before = flags.misheard; flags.misheard = false; editing = false; saved = false; if (!await post({ misheard: false })) { flags.misheard = before; paint(); } };
  const save = async () => {
    const text = input.value.trim();
    if (await post({ misheard: true, actual_text: text })) { editing = false; saved = true; paint(); }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.stopPropagation(); editing = false; paint(); }
  });

  function paint() {
    const marked = flags.misheard === true;
    const typing = document.activeElement === input, caret = typing ? [input.selectionStart, input.selectionEnd] : null;
    const why = problem && (problem.kind === 'missing' ? '서버에 인식 품질 기능이 설치되지 않아 표시를 저장하지 못했습니다.'
      : problem.kind === 'notfound' ? '서버가 아직 이 턴을 모릅니다. 답이 끝난 뒤 다시 시도하세요.'
      : problem.kind === 'unreachable' ? '서버에 연결할 수 없어 저장하지 못했습니다.' : '저장하지 못했습니다: ' + problem.message);
    setKids(node,
      el('div', { class: 'mh-line' },
        marked ? el('span', { class: 'mh-on' }, '잘못 알아들음으로 표시함') : null,
        marked ? null : el('button', { type: 'button', class: 'mh-btn mh-mark', disabled: busy || null, onclick: mark, 'aria-label': '잘못 알아들음: ' + about + '을 에이전트가 잘못 알아들었다고 표시', title: '이 말을 에이전트가 잘못 알아들었다고 표시합니다. 대화는 그대로 이어집니다.' }, '잘못 알아들음'),
        marked && !editing ? el('button', { type: 'button', class: 'mh-btn', onclick: () => { editing = true; input.value = flags.actual_text || ''; paint(); } }, flags.actual_text ? '고치기' : '실제 한 말 적기') : null,
        marked ? el('button', { type: 'button', class: 'mh-btn', disabled: busy || null, onclick: unmark, 'aria-label': about + '의 잘못 알아들음 표시 취소' }, '표시 취소') : null),
      marked && !editing && flags.actual_text ? el('div', { class: 'mh-actual' }, '실제 한 말: ', el('q', {}, flags.actual_text)) : null,
      marked && editing ? el('div', { class: 'mh-field' }, input,
        el('button', { type: 'button', class: 'small', disabled: busy || null, onclick: save }, '저장'),
        el('button', { type: 'button', class: 'ghost small', onclick: () => { editing = false; paint(); } }, '닫기')) : null,
      problem ? el('div', { class: 'mh-note bad-text', role: 'alert' }, why + ' ',
        problem.kind === 'missing' ? null : el('button', { type: 'button', class: 'mh-btn', onclick: () => post(pending) }, '다시 시도')) : null,
      !problem && marked && saved ? el('div', { class: 'mh-note' }, '저장했습니다.') : null,
      marked && opts.recorded === false ? el('div', { class: 'mh-note' }, '녹음하지 않는 대화라 음성은 남지 않습니다. 표시는 저장되지만 나중에 다시 들어 볼 수는 없습니다.') : null);
    // repainting takes the field out of the page for a moment: give the caret back to someone who was typing in it
    if (typing && input.isConnected) { input.focus({ preventScroll: true }); try { input.setSelectionRange(caret[0], caret[1]); } catch (err) { /* not selectable */ } }
  }
  paint();
  return { node, retry: () => { if (problem && pending && !busy) post(pending); }, failed: () => !!problem };
}

// ---- what the customer's screen was shown (docs/DISPLAY.md) --------------------------------------
const won = n => (n === null || n === undefined || isNaN(n)) ? '-' : Number(n).toLocaleString('ko-KR') + '원';
const DISPLAY_LABEL = { menu: '메뉴', items: '찾은 메뉴', cart: '주문 내역', confirm: '확인 카드', receipt: '영수증', notice: '안내' };
const cartLineText = l => (l.name || l.item_id || '') + ((l.options || []).length ? ' (' + l.options.join(', ') + ')' : '')
  + ' × ' + (l.quantity || 1) + (l.line_total != null ? ' · ' + won(l.line_total) : '');

/** One display payload in words: {summary, lines[]}. For the monitor's turn detail and the backstage record;
    the customer's screen itself is drawn by the conversation page. Unknown kinds give an empty summary. */
function displayText(kind, data) {
  data = data || {};
  const lines = (data.lines || []).map(cartLineText);
  if (kind === 'menu') {
    const cats = data.categories || [], all = cats.flatMap(c => c.items || []), sold = all.filter(i => i.sold_out).length;
    return { summary: '메뉴 전체: ' + cats.length + '종류, ' + all.length + '개' + (sold ? ' (품절 ' + sold + '개)' : ''),
      lines: cats.map(c => c.name + ' ' + (c.items || []).length + '개') };
  }
  if (kind === 'items') {
    const items = data.items || [];
    return { summary: (data.query ? '“' + data.query + '” ' : '') + '찾은 메뉴 ' + items.length + '개를 강조',
      lines: items.map(i => i.name + ' ' + won(i.price) + (i.sold_out ? ' (품절)' : '')) };
  }
  if (kind === 'cart') {
    if (data.discount) lines.push('할인 ' + won(data.discount) + (data.coupon ? ' (쿠폰 ' + data.coupon + ')' : ''));
    return { summary: lines.length ? '주문 내역 ' + (data.lines || []).length + '줄, 합계 ' + won(data.total) : '주문 내역 비어 있음', lines };
  }
  if (kind === 'confirm') {
    if (data.total != null) lines.push('합계 ' + won(data.total));
    return { summary: data.text || '실행 전 확인', lines };
  }
  if (kind === 'receipt') {
    if (data.message) lines.push(data.message);
    return { summary: '주문 번호 ' + (data.order_no || '-') + (data.total != null ? ', 합계 ' + won(data.total) : ''), lines };
  }
  if (kind === 'notice') return { summary: data.text || '', lines: [] };
  return { summary: '', lines: [] };
}
