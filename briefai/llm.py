"""OpenAI calls. Model names and key come from .env."""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from .config import ROOT
from .engine import Verdict


class Judgment(BaseModel):
    specific: bool
    reflection: str | None
    follow_up: str | None
    clarify: str | None


class Explanation(BaseModel):
    is_clarification: bool
    explanation: str | None


def _client(timeout: float) -> OpenAI:
    load_dotenv(ROOT / ".env")
    return OpenAI(timeout=timeout, max_retries=1)


def _max_tokens(call: str) -> int:
    """Output-token cap per call (settings.json → max_output_tokens), so a
    public endpoint can't run up an unbounded bill. Includes reasoning tokens."""
    return json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))["max_output_tokens"][call]


def make_judge():
    client = _client(timeout=45)
    model = os.environ["OPENAI_MODEL"]
    limit = _max_tokens("judge")

    def judge(system: str, exchange: str) -> Verdict:
        r = client.responses.parse(
            model=model,
            max_output_tokens=limit,
            input=[{"role": "system", "content": system}, {"role": "user", "content": exchange}],
            text_format=Judgment,
        )
        j = r.output_parsed
        return Verdict(specific=j.specific, reflection=j.reflection, follow_up=j.follow_up, clarify=j.clarify)

    return judge


def make_explain():
    """Called only when a reply looks like "what does this mean?" and the
    judge doesn't see it (choice follow-ups, edits, no budget left)."""
    client = _client(timeout=45)
    model = os.environ["OPENAI_MODEL"]
    limit = _max_tokens("judge")

    def explain(system: str, message: str) -> str | None:
        r = client.responses.parse(
            model=model,
            max_output_tokens=limit,
            input=[{"role": "system", "content": system}, {"role": "user", "content": message}],
            text_format=Explanation,
        )
        e = r.output_parsed
        return e.explanation if e.is_clarification else None

    return explain


def make_synthesis():
    """Returns (synthesize, write) for the brief and build prompt. OPENAI_MODEL_SYNTHESIS
    falls back to OPENAI_MODEL when empty."""
    from .synthesis import Synthesis

    client = _client(timeout=300)
    model = os.environ.get("OPENAI_MODEL_SYNTHESIS") or os.environ["OPENAI_MODEL"]
    limit = _max_tokens("synthesis")

    def synthesize(system: str, user: str) -> Synthesis:
        r = client.responses.parse(
            model=model,
            max_output_tokens=limit,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text_format=Synthesis,
        )
        return r.output_parsed

    def write(system: str, user: str) -> str:
        r = client.responses.create(
            model=model,
            max_output_tokens=limit,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return r.output_text

    return synthesize, write
