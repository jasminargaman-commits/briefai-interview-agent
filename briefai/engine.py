"""Interview engine. Stateless: every call takes the full State and returns
a new one. No I/O here — the CLI and the web API handle that."""

import json
import re
from typing import Any, Literal
from collections.abc import Callable

from pydantic import BaseModel

from .config import OTHER, Config

EMAIL = re.compile(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+")
RETURN_INTRO = "לפני שנסיים, נחזור לשאלות שדילגתם עליהן."
OTHER_MISSING = "כתבו במילים שלכם מה בדיוק."
EXPLAIN_FALLBACK = "אפשר לענות במילים שלכם, גם בקצרה. ואם אין לכם תשובה — אפשר לכתוב \"לא יודעים\" או לדלג."
# Cheap pre-check for "what does this mean?" — the LLM confirms before we explain.
CLARIFY_HINT = re.compile(r"מה (זה|זאת|הכוונה|אתם מתכוונים|את מתכוונת)|לא (הבנתי|הבנו|ברור)"
                          r"|למה הכוונה|אפשר להסביר|תסבירי")
REVIEW_TEXT = "רוצים לשנות משהו לפני שאני מסכמת?"


# ---------- State ----------

class Event(BaseModel):
    role: Literal["agent", "client", "system"]
    text: str
    qid: str | None = None


class FollowUp(BaseModel):
    source: Literal["judge", "choice"]
    question: str
    reflection: str | None = None  # judge: what was understood, shown before the question
    answer: str | None = None  # None = client skipped it
    key: str | None = None
    option: str | None = None
    dropped: bool = False  # choice follow-up whose option was deselected by an edit


class Answer(BaseModel):
    value: Any
    other: str | None = None  # free text when "אחר" was chosen
    follow_ups: list[FollowUp] = []


class Pending(BaseModel):
    qid: str
    source: Literal["judge", "choice"]
    question: str
    reflection: str | None = None
    key: str | None = None
    option: str | None = None


class State(BaseModel):
    answers: dict[str, Answer] = {}
    skipped: list[str] = []  # skipped once, in skip order (kept as a record)
    unanswered: list[str] = []  # skipped again on the return pass
    pending: list[Pending] = []  # follow-ups waiting; the first is asked now
    editing: str | None = None  # "qid" or "qid.n" (n = follow-up number)
    return_started: bool = False
    turns_used: int = 0
    confirmed: bool = False
    notice: str | None = None  # validation message shown with the next prompt
    explanation: str | None = None  # answer to "what does this mean?", shown with the same prompt
    started_at: float = 0  # web: when the interview was opened (bot check: min fill time)
    flagged: bool = False  # web: looks like a bot — no LLM calls, nothing sent
    demo: bool = False  # web: demo link (?demo) — the interview runs, finish writes and sends nothing
    id: str = ""  # web: random interview id — LLM call caps are counted by it, so replays don't reset them
    transcript: list[Event] = []


# ---------- Actions (what the client did) ----------

class Reply(BaseModel):
    value: Any
    other: str | None = None  # free text for "אחר", sent with the choice itself


class Skip(BaseModel):
    pass


class Edit(BaseModel):
    target: str


class Confirm(BaseModel):
    pass


Action = Reply | Skip | Edit | Confirm

class Verdict(BaseModel):
    specific: bool
    follow_up: str | None = None
    reflection: str | None = None  # short "what I understood", opens the follow-up
    clarify: str | None = None  # the client asked what the question means → explanation


# Judge: (system prompt, exchange so far) -> Verdict
Judge = Callable[[str, str], Verdict]
# Explain: (system prompt, question + client's message) -> explanation, or None
# when the message is an answer after all (the regex pre-check is loose).
Explain = Callable[[str, str], str | None]


# ---------- Prompt (what to show the client now) ----------

class Prompt(BaseModel):
    kind: Literal["form", "question", "follow_up", "review", "done"]
    qid: str | None = None
    text: str = ""
    hint: str | None = None
    options: list[str] = []
    option_hints: list[str] = []
    multi: bool = False
    exclusive: list[str] = []
    max: int | None = None
    intro: str | None = None
    editing: str | None = None
    current: str | None = None  # current answer, when editing
    notice: str | None = None
    reflection: str | None = None  # judge follow-up: what was understood so far
    explanation: str | None = None  # the client asked what the question means
    other: str | None = None  # current "אחר" text, when editing
    none: str | None = None  # open question: "nothing here" answer, sent as the reply value
    input: str = ""  # the question's type in questions.json (text, cards, colors, list…)
    previews: list[str] = []  # per option: illustration key (style cards)
    fields: list[dict] = []  # list question: the fields of one item
    add_label: str | None = None  # list question: "+ another item" button


# ---------- Queries ----------

def start() -> State:
    return State()


def _selected(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def relevant(state: State, cfg: Config, qid: str) -> bool:
    if not cfg.included(qid):
        return False
    cond = cfg.by_id[qid].get("showIf")
    if not cond:
        return True
    parent = state.answers.get(cond["id"])
    return bool(parent) and relevant(state, cfg, cond["id"]) and any(v in cond["in"] for v in _selected(parent.value))


def reached(state: State, qid: str) -> bool:
    return qid in state.answers or qid in state.skipped or qid in state.unanswered


def _main_queue(state: State, cfg: Config) -> list[str]:
    return [q for q in cfg.chat_ids if relevant(state, cfg, q) and not reached(state, q)]


def _deferred(state: State, cfg: Config) -> list[str]:
    return [q for q in cfg.chat_ids if q in state.skipped and q not in state.answers
            and q not in state.unanswered and relevant(state, cfg, q)]


def display(cfg: Config, qid: str, ans: Answer) -> str:
    return _event_text(ans.value, ans.other)


def _section(cfg: Config, qid: str) -> dict:
    return next(s for s in cfg.sections if s["id"] == cfg.by_id[qid]["section"])


def _question_prompt(state: State, cfg: Config, qid: str, kind: str = "question") -> Prompt:
    q = cfg.by_id[qid]
    opts = q.get("options", [])
    return Prompt(
        kind=kind,
        qid=qid,
        text=q["label"],
        hint=cfg.hint(qid),
        options=cfg.options(qid),
        option_hints=[o.get("hint", "") if isinstance(o, dict) else "" for o in opts]
        + [""] * (len(cfg.options(qid)) - len(opts)),
        exclusive=cfg.exclusive(qid),
        none=cfg.none_option(qid) if cfg.kind(qid) == "open" else None,
        multi=q["type"] == "multi",
        max=q.get("max"),
        input=q["type"],
        previews=[o.get("preview", "") if isinstance(o, dict) else "" for o in opts]
        + [""] * (len(cfg.options(qid)) - len(opts)),
        fields=q.get("fields", []),
        add_label=q.get("addLabel"),
    )


def next_prompt(state: State, cfg: Config) -> Prompt:
    p = _next_prompt(state, cfg)
    p.notice = state.notice
    p.explanation = state.explanation
    return p


def _next_prompt(state: State, cfg: Config) -> Prompt:
    if state.confirmed:
        return Prompt(kind="done")

    if state.editing:
        qid, _, n = state.editing.partition(".")
        if n:
            f = _visible_follow_ups(state.answers[qid])[int(n) - 1]
            return Prompt(kind="follow_up", qid=qid, text=f.question, editing=state.editing,
                          current=f.answer or "(דילגתם)")
        p = _question_prompt(state, cfg, qid)
        p.editing = qid
        if qid in state.answers:
            p.current = display(cfg, qid, state.answers[qid])
            p.other = state.answers[qid].other
        return p

    for i, fid in enumerate(cfg.form_ids):
        if fid not in state.answers:
            p = _question_prompt(state, cfg, fid, kind="form")
            if i == 0:
                p.intro = cfg.sections[0].get("lead")
            return p

    if state.pending:
        pend = state.pending[0]
        return Prompt(kind="follow_up", qid=pend.qid, text=pend.question, reflection=pend.reflection)

    main = _main_queue(state, cfg)
    if main:
        qid = main[0]
        p = _question_prompt(state, cfg, qid)
        sec = _section(cfg, qid)
        sec_chat = [x["id"] for x in sec["questions"] if cfg.kind(x["id"]) != "form"]
        if sec is not cfg.sections[0] and not any(reached(state, x) for x in sec_chat):
            p.intro = sec.get("lead")
        return p

    deferred = _deferred(state, cfg)
    if deferred:
        p = _question_prompt(state, cfg, deferred[0])
        if not state.return_started:
            p.intro = RETURN_INTRO
        return p

    return Prompt(kind="review", text=REVIEW_TEXT)


def progress(state: State, cfg: Config) -> list[dict]:
    """Per section: title, how many of its chat questions were reached, and
    whether the client is in it now."""
    p = next_prompt(state, cfg)
    returning = p.kind == "question" and not p.editing and p.qid in _deferred(state, cfg)
    active = p.qid if p.kind in ("question", "follow_up") and not p.editing and not returning else None
    out = []
    for sec in cfg.sections:
        qids = [q["id"] for q in sec["questions"] if q["id"] in cfg.chat_ids and relevant(state, cfg, q["id"])]
        if not qids:
            continue
        out.append({
            "id": sec["id"],
            "title": sec["title"],
            "short": sec.get("shortTitle") or sec["title"],
            "total": len(qids),
            "reached": sum(1 for q in qids if reached(state, q)),
            "current": bool(active) and cfg.by_id[active]["section"] == sec["id"],
        })
    return out


def _visible_follow_ups(ans: Answer) -> list[FollowUp]:
    return [f for f in ans.follow_ups if not f.dropped]


def answers_view(state: State, cfg: Config) -> list[dict]:
    """Every reached, relevant question in questionnaire order — for /answers and review."""
    rows = []
    for q in cfg.questions:
        qid = q["id"]
        if not reached(state, qid) or not relevant(state, cfg, qid):
            continue
        ans = state.answers.get(qid)
        rows.append({
            "id": qid,
            "label": q["label"],
            "value": display(cfg, qid, ans) if ans else None,
            "status": "answered" if ans else ("unanswered" if qid in state.unanswered else "skipped"),
            "follow_ups": [{"n": i + 1, "question": f.question, "answer": f.answer}
                           for i, f in enumerate(_visible_follow_ups(ans))] if ans else [],
        })
    return rows


# ---------- Submit ----------

def submit(state: State, cfg: Config, action: Action, judge: Judge, explain: Explain | None = None) -> State:
    s = state.model_copy(deep=True)
    s.notice = None
    s.explanation = None
    prompt = _next_prompt(s, cfg)

    if isinstance(action, Edit):
        return _start_edit(s, cfg, action.target)

    if isinstance(action, Confirm):
        if prompt.kind == "review":
            s.confirmed = True
            s.transcript.append(Event(role="system", text="הלקוח אישר את התשובות"))
        return s

    if isinstance(action, Skip):
        return _skip(s, cfg, prompt)

    return _reply(s, cfg, prompt, action, judge, explain)


def _start_edit(s: State, cfg: Config, target: str) -> State:
    qid, _, n = target.partition(".")
    if qid not in cfg.by_id or not reached(s, qid) or not relevant(s, cfg, qid):
        s.notice = f"אין שאלה בשם \"{target}\" שאפשר לערוך עכשיו."
        return s
    if n:
        ans = s.answers.get(qid)
        if not ans or not n.isdigit() or not 1 <= int(n) <= len(_visible_follow_ups(ans)):
            s.notice = f"אין שאלת המשך \"{target}\"."
            return s
    s.editing = target
    s.transcript.append(Event(role="system", text=f"עריכה: {target}", qid=qid))
    return s


def _skip(s: State, cfg: Config, prompt: Prompt) -> State:
    if prompt.kind == "form":
        s.notice = "זה שדה חובה — אי אפשר לדלג עליו."
        return s
    if prompt.editing:
        s.editing = None
        return s
    if prompt.kind in ("review", "done"):
        return s

    _record_question(s, prompt)
    s.transcript.append(Event(role="client", text="(דילוג)", qid=prompt.qid))

    if prompt.kind == "follow_up":
        pend = s.pending.pop(0)
        s.answers[pend.qid].follow_ups.append(FollowUp(
            source=pend.source, question=pend.question, reflection=pend.reflection, key=pend.key, option=pend.option))
        s.turns_used += 1
        return s

    qid = prompt.qid
    if qid in s.skipped:
        s.unanswered.append(qid)
        s.return_started = True
    else:
        s.skipped.append(qid)
    return s


def _normalize(cfg: Config, prompt: Prompt, raw: Any, other: str | None) -> tuple[Any, str | None, str | None]:
    """Returns (value, other text, error)."""
    if prompt.kind == "follow_up":
        text = str(raw or "").strip()
        return (text, None, None) if text else (None, None, "אפשר לכתוב תשובה, או לדלג.")

    q = cfg.by_id[prompt.qid]
    if q["type"] == "contact":
        if isinstance(raw, dict):
            phone, email = (raw.get("phone") or "").strip(), (raw.get("email") or "").strip()
        else:
            text = str(raw or "")
            m = EMAIL.search(text)
            email = m.group(0) if m else ""
            phone = re.sub(r"[^\d+\-]", "", text.replace(email, ""))
        if email and not EMAIL.fullmatch(email):
            return None, None, "כתובת המייל לא תקינה."
        if not phone and not email:
            return None, None, "צריך טלפון או מייל."
        return {"phone": phone, "email": email}, None, None

    if q["type"] == "colors":  # swatches and/or free text — e.g. ["טורקיז (#1aa6a6)", "#ff0000"]
        raw = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
        values = list(dict.fromkeys(str(v).strip() for v in raw if str(v).strip()))
        if not values:
            return None, None, "צריך לבחור צבע, או לדלג."
        return values[: q.get("max") or len(values)], None, None

    if q["type"] == "list" and isinstance(raw, list):  # e.g. [{"url": …, "note": …}]
        keys = [f["key"] for f in q.get("fields", [])]
        items = [{k: str(item.get(k) or "").strip() for k in keys} for item in raw if isinstance(item, dict)]
        items = [i for i in items if any(i.values())][: q.get("max") or len(items)]
        if not items:
            return None, None, "אפשר לכתוב תשובה, או לדלג."
        return items, None, None

    options = cfg.options(prompt.qid)
    if options:
        values = [str(v).strip() for v in _selected(raw) if str(v).strip()]
        if not values:
            return None, None, "צריך לבחור אפשרות, או לדלג."
        bad = [v for v in values if v not in options]
        if bad:
            return None, None, f"האפשרות \"{bad[0]}\" לא ברשימה."
        if q["type"] != "multi":
            if len(values) > 1:
                return None, None, "אפשר לבחור רק אפשרות אחת."
            value = values[0]
        else:
            values = list(dict.fromkeys(values))
            specific = [v for v in values if v not in cfg.exclusive(prompt.qid)]
            values = specific or values
            value = values[: q.get("max") or len(values)]
        if OTHER not in _selected(value):
            return value, None, None
        other = (other or "").strip()
        return (value, other, None) if other else (None, None, OTHER_MISSING)

    text = str(raw or "").strip()
    if not text:
        return None, None, "זה שדה חובה." if prompt.kind == "form" else "אפשר לכתוב תשובה, או לדלג."
    return text, None, None


def _reply(s: State, cfg: Config, prompt: Prompt, action: Reply, judge: Judge, explain: Explain | None) -> State:
    if prompt.kind in ("review", "done"):
        return s
    value, other, error = _normalize(cfg, prompt, action.value, action.other)
    if error:
        s.notice = error
        return s

    qid = prompt.qid
    _record_question(s, prompt)
    s.transcript.append(Event(role="client", text=_event_text(value, other), qid=qid))

    # Apply on a copy: if the client was really asking what the question means,
    # the reply is not an answer — explain and stay on the same question.
    t = s.model_copy(deep=True)
    verdict = _apply(t, cfg, prompt, value, other, judge)
    explanation = verdict.clarify if verdict else None
    textual = prompt.kind == "follow_up" or (cfg.kind(qid) == "open" and value != prompt.none)
    if not verdict and textual:
        explanation = _explain(cfg, prompt, value, explain)
    if explanation and explanation.strip():
        s.explanation = explanation.strip()
        s.transcript.append(Event(role="agent", text=s.explanation, qid=qid))
        return s
    return t


def _apply(s: State, cfg: Config, prompt: Prompt, value: Any, other: str | None, judge: Judge) -> Verdict | None:
    """Stores the reply. Returns the judge's verdict when the judge ran."""
    qid = prompt.qid

    # Edits: replace, no judge, no budget. A skipped question opened early is a
    # first answer, not an edit — it falls through to the normal path below.
    first_answer = False
    if prompt.editing:
        s.editing = None
        if "." in prompt.editing:
            n = int(prompt.editing.partition(".")[2])
            _visible_follow_ups(s.answers[qid])[n - 1].answer = value
            return None
        if qid in s.unanswered:
            s.unanswered.remove(qid)
        if qid in s.answers:
            s.answers[qid].value = value
            s.answers[qid].other = other
            if cfg.kind(qid) == "choice":
                _after_choice(s, cfg, qid, edited=True)
            return None
        first_answer = True

    if prompt.kind == "follow_up":
        pend = s.pending.pop(0)
        s.answers[qid].follow_ups.append(FollowUp(
            source=pend.source, question=pend.question, reflection=pend.reflection, answer=value,
            key=pend.key, option=pend.option))
        s.turns_used += 1
        return _maybe_judge(s, cfg, qid, judge) if pend.source == "judge" else None

    # Main question (form, first pass or return pass).
    if qid in s.skipped and not first_answer:
        s.return_started = True
    s.answers[qid] = Answer(value=value, other=other)
    if cfg.kind(qid) == "open" and value != prompt.none:  # "nothing here": no judge, no turn
        s.turns_used += 1
        return _maybe_judge(s, cfg, qid, judge)
    if cfg.kind(qid) == "choice":
        _after_choice(s, cfg, qid, edited=False)
    return None


def _record_question(s: State, prompt: Prompt) -> None:
    if prompt.intro:
        s.transcript.append(Event(role="agent", text=prompt.intro, qid=None))
    text = f"{prompt.reflection} {prompt.text}" if prompt.reflection else prompt.text
    s.transcript.append(Event(role="agent", text=text, qid=prompt.qid))


def _event_text(value: Any, other: str | None = None) -> str:
    if isinstance(value, dict):  # contact
        return " · ".join(x for x in (value.get("phone"), value.get("email")) if x)
    if isinstance(value, list) and value and isinstance(value[0], dict):  # list question: one line per item
        return "\n".join(" — ".join(v for v in item.values() if v) for item in value)
    return ", ".join(f"{OTHER}: {other}" if x == OTHER and other else str(x) for x in _selected(value))


# ---------- Clarification ----------

def _explain(cfg: Config, prompt: Prompt, text: str, explain: Explain | None) -> str | None:
    """Only for replies the judge doesn't see. The regex is a cheap pre-check;
    the LLM decides whether it's really a request to explain."""
    if not isinstance(text, str):
        return None
    hinted = bool(CLARIFY_HINT.search(text))
    if len(text) > 200 or not (hinted or text.rstrip().endswith("?")):
        return None
    try:
        if explain is None:
            raise RuntimeError("no explainer")
        return explain(_explain_system(cfg, prompt), f"שאלה: {prompt.text}\nהודעת הלקוח: {text}")
    except Exception:
        return EXPLAIN_FALLBACK if hinted else None


def _explain_system(cfg: Config, prompt: Prompt) -> str:
    intent = cfg.intents["questions"].get(prompt.qid, {})
    fields = {
        "general": json.dumps(cfg.intents.get("general", {}), ensure_ascii=False, indent=2),
        "question": prompt.text,
        "parent": cfg.by_id[prompt.qid]["label"],
        "hint": cfg.hint(prompt.qid) or "(none)",
        "intent": json.dumps({k: intent[k] for k in ("purpose", "good_answer") if k in intent},
                             ensure_ascii=False, indent=2) or "(none)",
    }
    text = cfg.explain_prompt
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    return text


# ---------- Follow-ups ----------

def _budget_left(s: State, cfg: Config) -> int:
    return cfg.settings["max_turns"] - s.turns_used - len(s.pending)


def _after_choice(s: State, cfg: Config, qid: str, edited: bool) -> None:
    ans = s.answers[qid]
    chosen = _selected(ans.value)

    if edited:  # drop follow-ups of options no longer chosen
        for f in ans.follow_ups:
            if f.source == "choice" and f.option not in chosen:
                f.dropped = True
        s.pending = [p for p in s.pending if p.qid != qid or p.option in chosen]

    new: list[Pending] = []
    rules = cfg.choice_follow_ups(qid)
    limit = cfg.intents.get("choice_follow_ups", {}).get("max_per_question", 2)
    used = {f.key for a in s.answers.values() for f in a.follow_ups if f.source == "choice" and not f.dropped}
    used |= {p.key for p in s.pending if p.key}
    count = sum(1 for f in ans.follow_ups if f.source == "choice" and not f.dropped)
    count += sum(1 for p in s.pending if p.qid == qid and p.source == "choice")
    budget = _budget_left(s, cfg)

    for opt in chosen:
        rule = rules.get(opt)
        if not rule or rule["key"] in used:
            continue
        if rule.get("skip_if_asked") and relevant(s, cfg, rule["skip_if_asked"]):
            continue
        if count >= limit or budget <= 0:
            break
        new.append(Pending(qid=qid, source="choice", question=rule["ask"], key=rule["key"], option=opt))
        used.add(rule["key"])
        count += 1
        budget -= 1

    s.pending.extend(new)


def _maybe_judge(s: State, cfg: Config, qid: str, judge: Judge) -> Verdict | None:
    if not cfg.judged(qid) or _budget_left(s, cfg) <= 0:
        return None
    ans = s.answers[qid]
    judge_count = sum(1 for f in ans.follow_ups if f.source == "judge")
    if judge_count >= cfg.settings["max_judge_follow_ups"]:
        return None
    if any(f.source == "judge" and f.answer is None for f in ans.follow_ups):
        return None  # client skipped a follow-up here — don't push

    try:
        v = judge(_judge_system(s, cfg, qid), _exchange(cfg, qid, ans))
    except Exception as e:  # never block the interview on the LLM
        s.transcript.append(Event(role="system", text=f"judge failed: {type(e).__name__}", qid=qid))
        return None
    s.transcript.append(Event(role="system", text=f"judge: specific={v.specific}" + (" clarify" if v.clarify else ""),
                              qid=qid))
    if not v.clarify and not v.specific and v.follow_up:
        s.pending.insert(0, Pending(qid=qid, source="judge", question=v.follow_up.strip(),
                                    reflection=(v.reflection or "").strip() or None))
    return v


def _exchange(cfg: Config, qid: str, ans: Answer) -> str:
    lines = [f"שאלה: {cfg.by_id[qid]['label']}", f"תשובה: {display(cfg, qid, ans)}"]
    for f in _visible_follow_ups(ans):
        lines += [f"שאלת המשך: {f.question}", f"תשובה: {f.answer if f.answer is not None else '(דילגו)'}"]
    return "\n".join(lines)


def _judge_system(s: State, cfg: Config, qid: str) -> str:
    q = cfg.by_id[qid]
    intent = cfg.intents["questions"][qid]
    general = cfg.intents.get("general", {})
    covered = [cfg.by_id[c]["label"] for c in intent.get("covered_later", []) if c in cfg.by_id]
    context = []  # other answers, with their follow-ups, so the judge doesn't ask again
    for k, a in s.answers.items():
        if k == qid or not relevant(s, cfg, k) or cfg.kind(k) == "form":
            continue
        context.append(f"- {cfg.by_id[k]['label']}: {display(cfg, k, a)}")
        context += [f"  - {f.question} → {f.answer}" for f in _visible_follow_ups(a) if f.answer]
    fields = {
        "general": json.dumps(general, ensure_ascii=False, indent=2),
        "question": f"{q['label']}\n({q.get('placeholder') or q.get('help') or ''})",
        "intent": json.dumps({k: v for k, v in intent.items() if k != "covered_later"}, ensure_ascii=False, indent=2),
        "covered_later": "\n".join(f"- {c}" for c in covered) or "(none)",
        "context": "\n".join(context) or "(none yet)",
    }
    text = cfg.judge_prompt
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    return text
