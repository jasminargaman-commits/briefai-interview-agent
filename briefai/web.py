"""Web chat API (FastAPI). The engine stays stateless — the browser holds the
state and sends it back every turn. The state is HMAC-signed so a client
can't edit it (e.g. reset the turn budget). The internal brief never goes
to the browser — the client sees their own answers ("האפיון שלכם")."""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engine, mail, synthesis
from .config import ROOT, load
from .engine import Confirm, Edit, Reply, Skip, State, Verdict

load_dotenv(ROOT / ".env")
cfg = load()
log = logging.getLogger("briefai")

def _secret() -> bytes:
    """STATE_SECRET from .env (required in production). Locally, falls back to a
    gitignored .state_secret file so open interviews survive server reloads."""
    if os.environ.get("STATE_SECRET"):
        return os.environ["STATE_SECRET"].encode()
    if os.environ.get("VERCEL"):
        raise RuntimeError("STATE_SECRET is required on Vercel")
    path = ROOT / ".state_secret"
    try:
        if not path.exists():
            path.write_text(secrets.token_hex(32))
        return path.read_text().strip().encode()
    except OSError:
        log.warning("STATE_SECRET not set — open interviews reset on restart.")
        return secrets.token_bytes(32)


SECRET = _secret()

MAX_BODY = 256 * 1024
TEXT_LIMIT = 3000
# A person needs longer than this from opening the page to sending the form
# (same check as the site's /brief). Faster → "send again"; honeypot → bot.
MIN_FILL_SECONDS = 5
ON_VERCEL = bool(os.environ.get("VERCEL"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_clients: dict = {}  # lazily created LLM callables, mailer and finish cache


def _judge():
    if "judge" not in _clients:
        from .llm import make_judge
        _clients["judge"] = make_judge()
    return _clients["judge"]


def _explain():
    if "explain" not in _clients:
        from .llm import make_explain
        _clients["explain"] = make_explain()
    return _clients["explain"]


def _mailer():
    if "mail" not in _clients:
        _clients["mail"] = mail.make_sender()
    return _clients["mail"]


def _finished():
    """Signatures of interviews already finished, so /api/finish runs once per
    interview. On Vercel: the Runtime Cache (shared by instances in a region);
    locally an in-memory fallback."""
    if "finished" not in _clients:
        from vercel.functions import RuntimeCache
        _clients["finished"] = RuntimeCache(namespace="briefai-finished")
    return _clients["finished"]


def _synthesis():
    if "synthesis" not in _clients:
        from .llm import make_synthesis
        _clients["synthesis"] = make_synthesis()
    return _clients["synthesis"]


# ---------- Signing ----------

def sign(state: State) -> str:
    return hmac.new(SECRET, state.model_dump_json().encode(), hashlib.sha256).hexdigest()


class Signed(BaseModel):
    state: dict
    sig: str


class ActionIn(BaseModel):
    type: Literal["reply", "skip", "edit", "confirm"]
    value: Any = None
    other: str | None = None
    target: str | None = None
    trap: str | None = None  # honeypot field of the opening form — people leave it empty


class StepIn(Signed):
    action: ActionIn


def verify(body: Signed) -> State:
    try:
        state = State.model_validate(body.state)
    except Exception:
        raise HTTPException(409, "השיחה לא תקינה — מתחילים מחדש.") from None
    if not hmac.compare_digest(sign(state), body.sig):
        raise HTTPException(409, "השיחה פגה — מתחילים מחדש.")
    return state


# ---------- View ----------

def view(state: State) -> dict:
    prompt = engine.next_prompt(state, cfg)
    out = {
        "state": state.model_dump(mode="json"),
        "sig": sign(state),
        "prompt": prompt.model_dump(),
        "chat": [{"role": e.role, "text": e.text, "qid": e.qid} for e in state.transcript
                 if e.role != "system" and e.qid not in cfg.form_ids],
        "answers": engine.answers_view(state, cfg),
        "sections": engine.progress(state, cfg),
        "section_of": {q["id"]: q["section"] for q in cfg.questions},
        "form_ids": cfg.form_ids,
    }
    if prompt.kind == "form":
        out["form"] = [{k: cfg.by_id[f].get(k) for k in ("id", "type", "label", "placeholder", "help")}
                       for f in cfg.form_ids]
    return out


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:TEXT_LIMIT]
    if isinstance(value, list):
        return [_clip(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:20]: _clip(v) for k, v in list(value.items())[:5] if isinstance(v, str)}
    return None


# ---------- Routes ----------

@app.middleware("http")
async def limit_body(request: Request, call_next):
    length = request.headers.get("content-length") or "0"
    if not length.isdigit() or int(length) > MAX_BODY:
        return JSONResponse({"detail": "הבקשה גדולה מדי."}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def only_from_site(request: Request, call_next):
    """On Vercel the agent answers only the site's /api/agent proxy, which sends
    the project's bypass secret (Deployment Protection → Protection Bypass for
    Automation; Vercel exposes it as VERCEL_AUTOMATION_BYPASS_SECRET). The
    project's public *.vercel.app address isn't covered by Vercel's protection,
    so the check lives here. No secret configured → closed, not open."""
    if ON_VERCEL:
        key = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET", "")
        sent = request.headers.get("x-briefai-key", "")
        if not key or not hmac.compare_digest(sent.encode(), key.encode()):
            return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


@app.get("/api/copy")
def copy():
    return cfg.copy


class StartIn(BaseModel):
    demo: bool = False


@app.post("/api/start")
def start(body: StartIn | None = None):
    state = engine.start()
    state.started_at = time.time()
    state.demo = bool(body and body.demo)
    return view(state)


@app.post("/api/view")
def current(body: Signed):
    return view(verify(body))


@app.post("/api/step")
def step(body: StepIn):
    state = verify(body)
    a = body.action
    action = {
        "reply": lambda: Reply(value=_clip(a.value), other=_clip(a.other)),
        "skip": Skip,
        "edit": lambda: Edit(target=str(a.target or "")[:40]),
        "confirm": Confirm,
    }[a.type]()
    if a.type == "reply" and engine.next_prompt(state, cfg).kind == "form":
        if a.trap:  # people can't see the field — a bot; it goes on, but nothing runs
            state.flagged = True
        elif time.time() - state.started_at < MIN_FILL_SECONDS:
            # Too fast for a person (or autofill + a quick click): ask to send
            # again rather than flag — a real client never loses an interview.
            state.notice = cfg.copy["welcome"]["too_fast"]
            return view(state)
    if a.type != "reply":
        return view(engine.submit(state, cfg, action, None))
    if state.flagged:  # the interview goes on as usual, but no LLM calls
        return view(engine.submit(state, cfg, action, _accept_all))
    return view(engine.submit(state, cfg, action, _judge(), _explain()))


def _accept_all(system: str, exchange: str) -> Verdict:
    return Verdict(specific=True)


@app.post("/api/finish")
async def finish(body: Signed, background: BackgroundTasks):
    """Accepts the confirmed interview and answers right away — the client sees
    "האפיון שלכם" built from the answers, not an LLM summary. Brief and build
    prompt are written in the background, once per interview. Bots get the
    same answer and nothing runs."""
    state = verify(body)
    if not state.confirmed:
        raise HTTPException(400, "הראיון עוד לא הסתיים.")
    if state.flagged:
        log.info("finish: flagged as bot, nothing sent")
        return {"ok": True}
    if state.demo:  # shared for feedback: no brief, no email
        return {"ok": True}
    finished = _finished()
    if await asyncio.to_thread(finished.get, body.sig):
        return {"ok": True}
    await asyncio.to_thread(finished.set, body.sig, True, {"ttl": 7 * 24 * 3600})
    if ON_VERCEL:  # keeps the function alive after the response (BackgroundTasks may not)
        from vercel.functions import wait_until
        wait_until(asyncio.to_thread(_finish_in_background, state, body.sig))
    else:
        background.add_task(_finish_in_background, state, body.sig)
    return {"ok": True}


def _finish_in_background(state: State, sig: str) -> None:
    """Brief + build prompt, then the email to Yasmin. Locally the files are also
    saved to transcripts/ (on Vercel the disk is read-only — the email is the copy).
    The answers go out even if the brief fails."""
    local = not ON_VERCEL
    out = None
    if local:
        business = re.sub(r"[^\w\-]+", "-", str(state.answers["business"].value)).strip("-")
        out = ROOT / "transcripts" / f"{datetime.now():%Y%m%d-%H%M%S}-web-{business}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "transcript.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")

    result, error = None, None
    try:
        synthesize, write = _synthesis()
        brief = synthesis.write_brief(state, cfg, synthesize)
        try:
            build = synthesis.write_build_prompt(brief, write)
        except Exception as e:
            build = f"(build prompt failed: {type(e).__name__})\n"
        result = synthesis.Result(brief=brief.brief, client_summary=brief.client_summary, build_prompt=build,
                                  scope_flags=brief.scope_flags, open_items=brief.open_items)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    if out:
        if result:
            synthesis.save(result, out)
        else:
            (out / "error.txt").write_text(f"brief failed: {error}\n", encoding="utf-8")

    # Client answers never go to the logs — only which interview failed (sig prefix).
    send = _mailer()
    if not send:
        if not local:
            log.error("MAIL not configured — interview briefai-%s not sent", sig[:12])
        return
    try:
        send(mail.compose(state, cfg, result, error), f"briefai-{sig[:40]}")
    except Exception as e:
        log.error("email failed for interview briefai-%s: %s", sig[:12], type(e).__name__)


if not ON_VERCEL:  # in production the chat page lives in the site (/briefai)
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
