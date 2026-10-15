// BriefAI web interview — one question per screen. The server is stateless:
// we hold the signed state and send it back each turn. All text goes in via
// textContent (no innerHTML), and no style="" attributes (the site's CSP) —
// only classes and CSSOM.

const KEY = 'briefai.v1';
// Where the agent's API lives: /api/ locally, /api/agent/ inside the site
// (<html data-api="/api/agent/">, the site's proxy to the agent project).
const API = document.documentElement.dataset.api || '/api/';
const OTHER = 'אחר';
const MIN_THINKING = 600;   // ms — no flash of the shimmer
const LATER_THINKING = 3000;
const LEAVE_MS = 150;

const $ = (id) => document.getElementById(id);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');

let view = null;      // last response from the server
let copy = null;      // copy.json
let sent = false;     // the server accepted the confirmed brief
let busy = false;
let welcomed = false;            // passed the welcome screen
let screen = null;               // { key, node } on the stage now
let lastActive = null;           // what was asked last render — animate only when it changes
let swapToken = 0;
let drafts = {};                 // unsent input per prompt — stored with the state, so it survives
                                 // a reload, a failed send, or going back to edit another answer

// ---------- helpers ----------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  return add(node, ...children);
}

// Like node.append, but skips false/null (native append would print them).
function add(node, ...children) {
  for (const c of children.flat(Infinity)) if (c) node.append(c);
  return node;
}

const fmt = (text, vars) => text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function load() { try { return JSON.parse(localStorage.getItem(KEY)); } catch { return null; } }
function store() {
  try { localStorage.setItem(KEY, JSON.stringify({ state: view.state, sig: view.sig, sent, drafts })); } catch {}
}

const draftKey = (p) => [p.kind, p.qid, p.editing || '', p.kind === 'follow_up' ? p.text : ''].join('|');
function saveDraft(p, patch) {
  drafts[draftKey(p)] = { ...drafts[draftKey(p)], ...patch };
  store();
}
function forget() { try { localStorage.removeItem(KEY); } catch {} }

async function api(path, body) {
  let res;
  try {
    res = await fetch(API + path, body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error('אין חיבור כרגע. מה שכתבתם נשמר — אפשר לנסות שוב.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 429: the site's rate limit (Vercel Firewall) — its answer isn't our JSON.
    const fallback = res.status === 429 ? copy?.too_many || 'יותר מדי ניסיונות ברצף. אפשר לנסות שוב בעוד כמה דקות.'
      : 'משהו השתבש. אפשר לנסות שוב.';
    const err = new Error(data.detail || fallback);
    err.status = res.status;
    throw err;
  }
  return data;
}

function setBusy(on) {
  busy = on;
  for (const b of document.querySelectorAll('#stage button, #stage textarea, #stage input, #answers button'))
    b.disabled = on;
}

const sectionOf = (qid) => qid && view.section_of[qid];
const sectionInfo = (id) => view.sections.find((s) => s.id === id);
const row = (qid) => view.answers.find((r) => r.id === qid);

// ---------- flow ----------

async function boot() {
  const params = new URLSearchParams(location.search);
  // ?demo: the link shared for feedback — runs as usual, nothing is sent. It stays
  // in the URL, so a reload keeps the demo; switching links starts a fresh interview.
  const demo = params.has('demo');
  // ?new starts a fresh interview (testing; the done screen has no restart button).
  if (params.has('new')) {
    forget();
    params.delete('new');
    history.replaceState(null, '', location.pathname + (params.size ? `?${params}` : ''));
  }
  let saved = load();
  if (saved && !!saved.state?.demo !== demo) {
    forget();
    saved = null;
  }
  sent = !!saved?.sent;
  drafts = saved?.drafts || {};
  try {
    copy = copy || await api('copy');
    view = saved ? await api('view', { state: saved.state, sig: saved.sig }) : await api('start', { demo });
  } catch {
    sent = false;
    view = await api('start', { demo });
  }
  if (saved) welcomed = true;
  store();
  render();
  if (view.prompt.kind === 'done' && !sent) await finish();
}

// `thinking`: show the shimmer while the LLM reads the answer (text replies only).
// `draft`: the draft key of what is being answered — cleared once the server has it.
async function send(action, { thinking = null, draft = null } = {}) {
  if (busy) return;
  setBusy(true);
  const started = Date.now();
  const later = thinking && setTimeout(() => { thinking.textContent = copy.thinking.later; }, LATER_THINKING);
  try {
    const next = await api('step', { state: view.state, sig: view.sig, action });
    if (thinking) await sleep(MIN_THINKING - (Date.now() - started));
    view = next;
    if (draft) delete drafts[draft];
    store();
    setBusy(false);
    render();
    if (view.prompt.kind === 'done' && !sent) await finish();
  } catch (e) {
    setBusy(false);
    fail(e);
  } finally {
    clearTimeout(later);
  }
}

async function finish() {
  render();  // the done screen shows "שולחת…" until the server has the brief
  try {
    await api('finish', { state: view.state, sig: view.sig });
    sent = true;
    forget();  // sent → nothing stays in this browser (as the privacy page says)
    render();
  } catch (e) {
    show('finish-failed', el('section', { class: 'screen' },
      el('p', { class: 'notice', text: `${copy.sent.failed} (${e.message})` }),
      el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'button', text: 'לנסות שוב', onclick: finish }))));
  }
}

function fail(e) {
  if (e.status === 409) {
    show('expired', el('section', { class: 'screen' },
      el('p', { text: e.message }),
      el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'button', text: 'התחלה מחדש', onclick: restart }))));
    return;
  }
  render();  // restore the input that was locked while sending
  const where = screen?.node.querySelector('.active') || screen?.node;
  where?.append(el('p', { class: 'notice', role: 'alert', text: e.message }));
}

async function restart() {
  forget();
  sent = false;
  drafts = {};
  welcomed = false;
  $('answers').hidden = true;
  await boot();
}

// ---------- screens ----------

function render() {
  const p = view.prompt;
  let key, build;
  if (p.kind === 'form') {
    const fresh = !Object.keys(view.state.answers).length;
    [key, build] = !welcomed && fresh ? ['welcome', welcomeScreen] : ['form', formScreen];
  } else if (p.kind === 'question' || p.kind === 'follow_up') {
    [key, build] = [`q:${p.qid}:${p.editing || ''}`, () => questionScreen(p)];
  } else if (p.kind === 'review') {
    [key, build] = ['review', reviewScreen];
  } else {
    [key, build] = ['done', doneScreen];
  }
  renderTop(p, key);
  renderAnswersPanel(p, key);
  show(key, build());
}

// Same key → update in place (a follow-up slides in under the locked answer).
// New key → the old screen leaves, the new one enters.
function show(key, node) {
  const stage = $('stage');
  const token = ++swapToken;
  node.classList.add('screen');
  node.tabIndex = -1;
  if (screen && screen.key === key) {
    screen = { key, node };
    stage.replaceChildren(node);
    focusInput(node);
    node.querySelector('.slide-in')?.scrollIntoView({ block: 'nearest', behavior: reduced.matches ? 'auto' : 'smooth' });
    return;
  }
  const old = screen?.node;
  screen = { key, node };
  const enter = () => {
    if (token !== swapToken) return;
    node.classList.add('enter');
    stage.replaceChildren(node);
    window.scrollTo(0, 0);
    focusInput(node) || node.focus({ preventScroll: true });
  };
  if (old?.isConnected) {
    old.classList.add('leave');
    setTimeout(enter, LEAVE_MS);
  } else enter();
}

function focusInput(node) {
  const input = node.querySelector('.active textarea, .other-row:not([hidden]) input, form input');
  if (!input || matchMedia('(pointer: coarse)').matches) return false;  // no surprise keyboard on phones
  input.focus({ preventScroll: true });
  return true;
}

function welcomeScreen() {
  const w = copy.welcome;
  return el('section', { class: 'welcome' },
    view.state.demo && el('p', { class: 'demo-note', text: copy.demo.notice }),
    el('h1', { class: 'question', text: w.title }),
    el('p', { class: 'lead', text: w.lead }),
    el('ul', { class: 'points' }, w.points.map((t) => el('li', { text: t }))),
    el('div', { class: 'actions' },
      el('button', { class: 'btn', type: 'button', text: w.start, onclick: () => { welcomed = true; render(); } })));
}

function formScreen() {
  const answers = view.state.answers;
  const typed = drafts.form || {};  // what was typed, kept when the server answers with a notice
  const fields = {};
  const rows = view.form.map((f) => {
    if (f.type === 'contact') {
      const cur = typed.contact || answers.contact?.value || {};
      fields.phone = el('input', { type: 'tel', autocomplete: 'tel', dir: 'ltr', value: cur.phone || '' });
      fields.email = el('input', { type: 'email', autocomplete: 'email', dir: 'ltr', value: cur.email || '' });
      return el('div', {},
        el('div', { class: 'pair' },
          el('label', {}, 'טלפון', fields.phone),
          el('label', {}, 'מייל', fields.email)),
        f.help && el('span', { class: 'help-text', text: f.help }));
    }
    fields[f.id] = el('input', { type: 'text', placeholder: f.placeholder || '', value: typed[f.id] ?? answers[f.id]?.value ?? '',
      autocomplete: f.id === 'name' ? 'name' : f.id === 'business' ? 'organization' : 'off' });
    return el('label', {}, f.label, fields[f.id]);
  });
  const notice = el('p', { class: 'notice', role: 'alert', text: view.prompt.notice || '', hidden: !view.prompt.notice });
  // Honeypot: hidden from people, bots fill it in (same as the site's /brief).
  const trap = el('input', { type: 'text', name: 'company_website', tabindex: '-1', autocomplete: 'off' });

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    const values = {
      name: fields.name.value.trim(),
      business: fields.business.value.trim(),
      contact: { phone: fields.phone.value.trim(), email: fields.email.value.trim() },
    };
    drafts.form = values;
    store();
    try {
      while (view.prompt.kind === 'form') {
        const value = values[view.prompt.qid];
        view = await api('step', { state: view.state, sig: view.sig, action: { type: 'reply', value, trap: trap.value } });
        store();
        if (view.prompt.notice) break;
      }
      if (view.prompt.kind !== 'form') delete drafts.form;
      store();
      setBusy(false);
      render();
    } catch (err) {
      setBusy(false);
      fail(err);
    }
  }

  return el('section', {},
    el('h1', { class: 'question', text: copy.welcome.form_title }),
    el('form', { class: 'form active', onsubmit: submit }, rows,
      el('label', { class: 'trap', 'aria-hidden': 'true' }, 'Website', trap), notice,
      el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'submit', text: copy.buttons.next })),
      el('p', { class: 'consent' }, `${copy.welcome.consent} `,
        el('a', { href: copy.welcome.privacy_url, target: '_blank', rel: 'noopener', text: copy.welcome.privacy_link }))));
}

// The first question of a topic opens with a short intro: ✓ the topic just
// finished + encouragement, then the new topic's name and lead. Before the
// return pass, the intro is the "back to skipped questions" line.
function topicIntro(p) {
  if (!p.intro || p.editing) return null;
  if (view.state.skipped.includes(p.qid)) return el('p', { class: 'topic-intro return', text: p.intro });
  const order = view.sections.map((s) => s.id);
  const finished = copy.topics[order[order.indexOf(sectionOf(p.qid)) - 1]];
  return el('div', { class: 'topic-intro' },
    finished && el('p', { class: 'topic-done' },
      el('span', { class: 'check', 'aria-hidden': 'true', text: '✓' }),
      el('span', { text: `${finished.done}. ${finished.encouragement}` })),
    el('p', { class: 'eyebrow', text: sectionInfo(sectionOf(p.qid))?.title }),
    el('p', { class: 'topic-lead', text: p.intro }));
}

function questionScreen(p) {
  const r = row(p.qid);
  const followUp = p.kind === 'follow_up';
  const editingMain = p.editing && !p.editing.includes('.');
  const help = !followUp && copy.questions[p.qid];

  const screenNode = el('section', { class: followUp ? 'is-follow-up' : '' });
  const active = el('div', { class: 'active' });

  if (p.editing) {
    screenNode.append(el('div', { class: 'editing-bar' },
      el('span', { text: p.current ? `עריכה — התשובה הנוכחית: ${p.current}` : 'חוזרים לשאלה שדילגתם עליה' }),
      el('button', { class: 'link', type: 'button', text: copy.buttons.cancel, onclick: () => send({ type: 'skip' }) })));
  }

  if (followUp) {
    // The main question shrinks to a label; the follow-up is what stands out.
    add(screenNode, el('p', { class: 'parent-q', text: r?.label }));
    if (r && !p.editing) {
      screenNode.append(el('div', { class: 'thread' },
        answerCard(r.value, r.status !== 'answered', p.qid),
        r.follow_ups.map((f) => el('div', { class: 'thread-item' },
          el('p', { class: 'fu-q', text: f.question }),
          answerCard(f.answer, f.answer == null, `${p.qid}.${f.n}`)))));
    }
    active.append(el('div', {},
      p.reflection && el('p', { class: 'reflection', text: p.reflection }),
      el('h1', { class: 'question follow-up', text: p.text })));
  } else {
    add(screenNode,
      topicIntro(p),
      el('h1', { class: 'question', text: p.text }),
      p.hint && p.input !== 'list' && el('p', { class: 'hint', text: p.hint }),  // list: the fields say it
      help?.why && el('details', { class: 'help' },
        el('summary', { text: copy.help_label }),
        el('div', { class: 'body' },
          el('p', { text: help.why }),
          help.example && el('p', { class: 'example', text: help.example }))));
  }
  if (p.explanation) active.append(el('p', { class: 'explain', text: p.explanation }));

  const asked = `${p.kind}|${p.qid}|${p.text}|${p.explanation || ''}`;
  if (lastActive && lastActive !== asked && (followUp || p.explanation)) active.classList.add('slide-in');
  lastActive = asked;

  const current = editingMain ? view.state.answers[p.qid] : null;
  const editedFollowUp = followUp && p.editing && r?.follow_ups[Number(p.editing.split('.')[1]) - 1];
  const isNone = p.none && current?.value === p.none;
  if (p.options.length) active.append(choiceInput(p, current));
  else if (p.input === 'colors' && !followUp) active.append(colorsInput(p, current));
  else {
    // "אין לי": answers without text — not a skip, so it isn't asked again at the end.
    if (p.none && !followUp) add(active,
      el('div', {}, el('button', { class: 'chip', type: 'button', 'aria-pressed': String(isNone), text: p.none,
        onclick: () => send({ type: 'reply', value: p.none }) })),
      el('p', { class: 'none-hint', text: copy.none_hint }));
    const prefill = editedFollowUp ? editedFollowUp.answer : isNone ? null : current?.value;
    active.append(p.input === 'list' && !followUp
      ? listInput(p, active, prefill)
      : textInput(p, active, drafts[draftKey(p)]?.text ?? prefill));
  }
  if (p.notice) active.append(el('p', { class: 'notice', role: 'alert', text: p.notice }));
  active.append(actionsRow(p));

  screenNode.append(active);
  return screenNode;
}

function answerCard(value, missing, editTarget) {
  const text = Array.isArray(value)
    ? value.map((v) => (typeof v === 'object' ? Object.values(v).filter(Boolean).join(' — ') : v)).join('\n')
    : value;
  return el('div', { class: 'answer-card' },
    el('span', { class: `text${missing ? ' missing' : ''}`, text: missing ? 'דילגתם' : text }),
    el('button', { class: 'link', type: 'button', text: copy.buttons.edit,
      onclick: () => send({ type: 'edit', target: editTarget }) }));
}

// "חזרה" = edit the previous question; while editing it cancels instead (in the bar).
function actionsRow(p) {
  const back = !p.editing && backTarget(p);
  return el('div', { class: 'actions' },
    back && el('button', { class: 'link', type: 'button', text: copy.buttons.back,
      onclick: () => send({ type: 'edit', target: back }) }),
    !p.editing && el('button', { class: 'link', type: 'button', text: copy.buttons.skip,
      onclick: () => send({ type: 'skip' }) }),
    p.submit);
}

function backTarget(p) {
  const order = Object.keys(view.section_of);
  const at = order.indexOf(p.qid);
  const before = view.answers.filter((r) => !view.form_ids.includes(r.id) && order.indexOf(r.id) < at);
  return before.at(-1)?.id || null;
}

// Lock the answer and show the shimmer while the judge reads it.
function sendText(p, active, value, shown) {
  const thinking = el('p', { class: 'thinking', role: 'status', text: copy.thinking.first });
  active.replaceChildren(el('div', { class: 'answer-card' }, el('span', { class: 'text', text: shown })), thinking);
  send({ type: 'reply', value }, { thinking, draft: draftKey(p) });
}

function textInput(p, active, prefill) {
  const ta = el('textarea', { rows: 3, placeholder: 'כתבו כאן…', 'aria-label': 'התשובה שלכם' });
  ta.value = typeof prefill === 'string' ? prefill : '';
  ta.addEventListener('input', () => saveDraft(p, { text: ta.value }));
  const submit = () => {
    const value = ta.value.trim();
    if (value && !busy) sendText(p, active, value, value);
  };
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.isComposing) { e.preventDefault(); submit(); }
  });
  p.submit = el('button', { class: 'btn', type: 'button', text: copy.buttons.send, onclick: submit });
  return ta;
}

// A list question (e.g. sites they like): one set of fields per item, "+ עוד".
function listInput(p, active, prefill) {
  const max = p.max || 5;
  const start = drafts[draftKey(p)]?.items
    ?? (Array.isArray(prefill) ? prefill : typeof prefill === 'string' && prefill ? [{ [p.fields.at(-1).key]: prefill }] : [{}]);
  const items = start.map((i) => ({ ...i }));
  const box = el('div', { class: 'list-items' });
  const more = el('button', { class: 'link add-item', type: 'button', text: `+ ${p.add_label || 'עוד'}` });
  const save = () => saveDraft(p, { items });

  const draw = () => {
    box.replaceChildren(...items.map((item, n) => el('div', { class: 'list-item' },
      p.fields.map((f) => {
        const input = el(f.type === 'textarea' ? 'textarea' : 'input', {
          type: f.type === 'textarea' ? null : f.inputType === 'url' ? 'url' : 'text',
          rows: f.rows || null, dir: f.dir || null, placeholder: f.placeholder || '',
        });
        input.value = item[f.key] || '';
        input.addEventListener('input', () => { item[f.key] = input.value; save(); });
        return el('label', {}, el('span', { class: 'field-label', text: f.label }), input);
      }),
      items.length > 1 && el('button', { class: 'link remove-item', type: 'button', text: 'הסרה',
        onclick: () => { items.splice(n, 1); save(); draw(); } }))));
    more.hidden = items.length >= max;
  };
  more.addEventListener('click', () => { items.push({}); save(); draw(); box.lastElementChild?.querySelector('input, textarea')?.focus(); });
  draw();

  const submit = () => {
    const value = items.map((i) => Object.fromEntries(p.fields.map((f) => [f.key, (i[f.key] || '').trim()])))
      .filter((i) => Object.values(i).some(Boolean));
    if (!value.length || busy) return;
    sendText(p, active, value, value.map((i) => Object.values(i).filter(Boolean).join(' — ')).join('\n'));
  };
  p.submit = el('button', { class: 'btn', type: 'button', text: copy.buttons.send, onclick: submit });
  return el('div', { class: 'list' }, box, more);
}

// Choices are cards: select, see what's selected, then "המשך" — the same
// rhythm for single and multi choice.
function choiceInput(p, current) {
  const saved = drafts[draftKey(p)];
  const selected = current ? (Array.isArray(current.value) ? current.value : [current.value]) : [];
  const chosen = [...(saved?.chosen ?? selected)];
  const otherInput = el('input', { type: 'text', placeholder: 'מה בדיוק?', 'aria-label': 'אחר — מה בדיוק?',
    value: saved?.other ?? current?.other ?? '' });
  otherInput.addEventListener('input', () => saveDraft(p, { other: otherInput.value }));
  const otherRow = el('div', { class: 'other-row', hidden: !chosen.includes(OTHER) }, otherInput);

  const submit = () => {
    if (!chosen.length || busy) return;
    const other = otherInput.value.trim();
    if (chosen.includes(OTHER) && !other) return otherInput.focus();
    send({ type: 'reply', value: p.multi ? chosen : chosen[0], other: chosen.includes(OTHER) ? other : null },
      { draft: draftKey(p) });
  };
  otherInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit(); }
  });

  const visual = p.previews.some(Boolean);
  const buttons = p.options.map((opt, i) => el('button', {
    class: `card${p.multi ? ' multi' : ''}`, type: 'button', 'aria-pressed': String(chosen.includes(opt)), 'data-value': opt,
    onclick: () => {
      if (p.multi) {
        const at = chosen.indexOf(opt);
        if (at >= 0) chosen.splice(at, 1);
        else {
          // "לא, תודה" / "עוד לא יודעים" can't be combined with other options
          const keep = p.exclusive.includes(opt) ? [] : chosen.filter((c) => !p.exclusive.includes(c));
          chosen.splice(0, chosen.length, ...keep);
          if (!p.max || chosen.length < p.max) chosen.push(opt);
        }
      } else chosen.splice(0, chosen.length, opt);
      saveDraft(p, { chosen: [...chosen] });
      for (const b of buttons) b.setAttribute('aria-pressed', String(chosen.includes(b.dataset.value)));
      otherRow.hidden = !chosen.includes(OTHER);
      next.disabled = !chosen.length;
      if (!otherRow.hidden) otherInput.focus();
    },
  },
  visual && (p.previews[i] ? stylePreview(p.previews[i]) : el('span', { class: 'preview none', 'aria-hidden': 'true', text: '?' })),
  el('span', { class: 'mark', 'aria-hidden': 'true' }),
  el('span', { class: 'card-text' },
    el('span', { class: 'card-label', text: opt }),
    p.option_hints[i] && el('span', { class: 'card-hint', text: p.option_hints[i] }))));

  const next = el('button', { class: 'btn', type: 'button', text: copy.buttons.next, disabled: !chosen.length, onclick: submit });
  p.submit = next;
  return el('div', { class: 'choice' },
    el('div', { class: `cards${visual ? ' visual' : ''}`, role: 'group', 'aria-label': p.text }, buttons),
    otherRow,
    p.multi && el('p', { class: 'note', text: p.max ? `אפשר לבחור עד ${p.max}` : 'אפשר לבחור כמה' }));
}

// A tiny page sketch per style direction — built from divs, styled by class.
function stylePreview(key) {
  return el('span', { class: `preview ${key}`, 'aria-hidden': 'true' },
    el('span', { class: 'pv-nav' }, el('i'), el('i'), el('i')),
    el('span', { class: 'pv-title' }),
    el('span', { class: 'pv-line' }),
    el('span', { class: 'pv-line short' }),
    el('span', { class: 'pv-btn' }));
}

// Colors: palette swatches, the native color picker, or a name / code typed in.
function colorsInput(p, current) {
  const c = copy.colors;
  const max = p.max || 4;
  const saved = drafts[draftKey(p)];
  const chosen = [...(saved?.chosen ?? (Array.isArray(current?.value) ? current.value : []))];
  const labelOf = (s) => `${s.name} (${s.hex})`;
  const save = () => saveDraft(p, { chosen: [...chosen] });

  const toggle = (value) => {
    const at = chosen.indexOf(value);
    if (at >= 0) chosen.splice(at, 1);
    else if (chosen.length < max) chosen.push(value);
    save();
    draw();
  };

  const swatches = c.palette.map((s) => {
    const dot = el('span', { class: 'dot', 'aria-hidden': 'true' });
    dot.style.backgroundColor = s.hex;  // CSSOM, not a style="" attribute
    return el('button', { class: 'swatch', type: 'button', 'data-value': labelOf(s), title: s.name,
      onclick: () => toggle(labelOf(s)) }, dot, el('span', { class: 'swatch-name', text: s.name }));
  });

  const picker = el('input', { type: 'color', value: '#1aa6a6', 'aria-label': c.custom });
  picker.addEventListener('change', () => { if (!chosen.includes(picker.value)) toggle(picker.value); });
  const text = el('input', { type: 'text', placeholder: c.text_placeholder, 'aria-label': c.text_placeholder });
  const addText = () => { const v = text.value.trim(); if (v && !chosen.includes(v)) toggle(v); text.value = ''; };
  text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); addText(); } });

  const picked = el('div', { class: 'picked', 'aria-live': 'polite' });
  const next = el('button', { class: 'btn', type: 'button', text: copy.buttons.next,
    onclick: () => { if (chosen.length && !busy) send({ type: 'reply', value: chosen }, { draft: draftKey(p) }); } });

  function draw() {
    for (const b of swatches) b.setAttribute('aria-pressed', String(chosen.includes(b.dataset.value)));
    picked.replaceChildren(...chosen.map((v) => {
      const hex = v.match(/#[0-9a-f]{3,8}/i)?.[0];
      const dot = el('span', { class: 'dot', 'aria-hidden': 'true' });
      if (hex) dot.style.backgroundColor = hex;
      // Codes are LTR inside Hebrew text — isolate them so "#1aa6a6" doesn't flip.
      const name = v.replace(/\s*\(?#[0-9a-f]{3,8}\)?/i, '').trim();
      return el('button', { class: 'chip', type: 'button', 'aria-label': `הסרה: ${v}`, onclick: () => toggle(v) },
        dot, name && el('span', { text: name }), hex && el('bdi', { dir: 'ltr', class: 'code', text: hex }),
        !hex && !name && el('span', { text: v }),
        el('span', { class: 'x', 'aria-hidden': 'true', text: '×' }));
    }));
    next.disabled = !chosen.length;
  }
  draw();
  p.submit = next;
  return el('div', { class: 'colors' },
    el('div', { class: 'swatches', role: 'group', 'aria-label': p.text }, swatches),
    el('div', { class: 'custom-color' },
      el('label', { class: 'picker' }, picker, el('span', { text: c.custom })),
      el('div', { class: 'other-row' }, text, el('button', { class: 'btn secondary', type: 'button', text: c.add, onclick: addText }))),
    picked);
}

// "האפיון שלכם": the client's own answers by topic — no LLM, instant. Skipped
// questions stay in their topic as "להשלמה בשיחה". Editing returns here.
function reviewScreen() {
  const rv = copy.review;
  const byId = Object.fromEntries(view.answers.map((r) => [r.id, r]));
  const edit = (target, text = copy.buttons.edit) =>
    el('button', { class: 'link', type: 'button', text, onclick: () => send({ type: 'edit', target }) });
  const item = (r) => {
    const missing = r.status !== 'answered';
    return el('div', { class: 'brief-item' },
      el('div', { class: 'brief-head' },
        el('span', { class: 'brief-q', text: r.label }),
        edit(r.id, missing ? 'לענות עכשיו' : copy.buttons.edit)),
      missing ? el('span', { class: 'later', text: rv.skipped }) : el('p', { class: 'brief-a', text: r.value }),
      r.follow_ups.filter((f) => f.answer != null).map((f) => el('div', { class: 'brief-fu' },
        el('div', { class: 'brief-head' },
          el('span', { class: 'brief-q', text: f.question }),
          edit(`${r.id}.${f.n}`)),
        el('p', { class: 'brief-a', text: f.answer }))));
  };
  const block = (title, ids) => {
    const rows = ids.map((id) => byId[id]).filter(Boolean);
    return rows.length && el('section', { class: 'brief-section' }, el('h2', { text: title }), rows.map(item));
  };
  return el('section', { class: 'review' },
    el('h1', { class: 'question', text: rv.title }),
    el('p', { class: 'lead', text: rv.lead }),
    block(rv.header_title, rv.header),
    rv.sections.map((sec) => block(sec.title, sec.questions)),
    el('div', { class: 'send-box' },
      el('p', { class: 'note', text: rv.next_steps }),
      el('button', { class: 'btn', type: 'button', text: rv.send, onclick: () => send({ type: 'confirm' }) })));
}

function doneScreen() {
  const s = copy.sent;
  if (!sent) return el('section', {}, el('p', { class: 'thinking', role: 'status', text: s.sending }));
  if (view.state.demo) return el('section', { class: 'sent' },
    el('p', { class: 'done-mark', text: copy.demo.title }),
    el('p', { class: 'lead', text: copy.demo.text }));
  return el('section', { class: 'sent' },
    el('p', { class: 'done-mark', text: fmt(s.title, { name: String(view.state.answers.name?.value || '').split(' ')[0] }) }),
    el('p', { class: 'lead', text: s.text }));
}

// ---------- top bar ----------

function renderTop(p, key) {
  const intro = key === 'welcome' || key === 'form';
  $('progress').hidden = intro;
  if (intro) return;
  const steps = $('steps');
  steps.style.setProperty('--n', view.sections.length);  // CSSOM, not a style="" attribute
  // After the main pass (all topics reached), returning to skipped questions
  // doesn't move the bar back to an earlier topic.
  const finished = p.kind === 'review' || p.kind === 'done' || view.sections.every((s) => s.reached === s.total);
  const currentId = finished ? null : sectionOf(p.qid);
  const at = view.sections.findIndex((s) => s.id === currentId);
  steps.replaceChildren(...view.sections.map((s) => {
    const done = finished || (s.reached === s.total && s.id !== currentId);
    const state = s.id === currentId ? 'is-current' : done ? 'is-done' : '';
    return el('li', { class: state, 'aria-current': s.id === currentId ? 'step' : false },
      el('span', { class: 'sr', text: `${s.title}${done ? ' ✓' : ''}` }));
  }));
  const label = $('progress-label');
  if (at < 0) {
    label.textContent = p.kind === 'review' || p.kind === 'done' ? copy.review.title : copy.progress_return;
    return;
  }
  const [topic, rest] = fmt(copy.progress, { topic: '\u0000', n: at + 1, total: view.sections.length }).split('\u0000');
  label.replaceChildren(topic, el('strong', { text: view.sections[at].title }), rest);
}

// ---------- answers panel ----------

function answersList() {
  const edit = (target) => () => { $('answers').hidden = true; toggleState(); send({ type: 'edit', target }); };
  return el('ul', { class: 'answer-list' }, view.answers.map((r) => {
    const missing = r.status !== 'answered';
    return el('li', { class: 'answer' },
      el('div', { class: 'head' },
        el('span', { class: 'q', text: r.label }),
        el('button', { class: 'link', type: 'button', text: missing ? 'לענות עכשיו' : copy.buttons.edit, onclick: edit(r.id) })),
      el('div', { class: `a${missing ? ' missing' : ''}`,
        text: missing ? (r.status === 'skipped' ? 'דילגתם' : 'לא נענה') : r.value }),
      r.follow_ups.map((f) => el('div', { class: 'fu' },
        el('div', { class: 'head' },
          el('span', { class: 'q', text: f.question }),
          el('button', { class: 'link', type: 'button', text: copy.buttons.edit, onclick: edit(`${r.id}.${f.n}`) })),
        el('div', { class: `a${f.answer == null ? ' missing' : ''}`, text: f.answer ?? 'דילגתם' }))));
  }));
}

function renderAnswersPanel(p, key) {
  const count = view.answers.length;
  const toggle = $('answers-toggle');
  toggle.hidden = key === 'welcome' || key === 'form' || p.kind === 'review' || p.kind === 'done' || count === 0;
  $('answers-count').textContent = `(${count})`;
  $('answers-list').replaceChildren(answersList());
}

function toggleState() { $('answers-toggle').setAttribute('aria-expanded', String(!$('answers').hidden)); }

$('answers-toggle').addEventListener('click', () => { $('answers').hidden = !$('answers').hidden; toggleState(); });
$('answers-close').addEventListener('click', () => { $('answers').hidden = true; toggleState(); $('answers-toggle').focus(); });
$('restart').addEventListener('click', () => { if (confirm('להתחיל מחדש? התשובות יימחקו.')) restart(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('answers').hidden) $('answers-close').click(); });

boot();
