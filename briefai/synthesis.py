"""Finished interview → brief, client summary and build prompt.
Facts (contact line, stats, full answers, fixed texts) are rendered here in
code; the LLM writes only the synthesis."""

import re
from datetime import date
from pathlib import Path
from collections.abc import Callable

from pydantic import BaseModel

from . import engine
from .config import ROOT, Config
from .engine import State


class ScopeFlag(BaseModel):
    item: str
    detail: str


class Synthesis(BaseModel):
    brief_markdown: str
    client_summary: str
    scope_flags: list[ScopeFlag]
    open_items: list[str]


class Result(BaseModel):
    brief: str  # full internal brief: header + synthesis + appendix
    client_summary: str
    build_prompt: str
    scope_flags: list[ScopeFlag]
    open_items: list[str]


# (system, user) -> Synthesis / str
Synthesize = Callable[[str, str], Synthesis]
Write = Callable[[str, str], str]


# ---------- Templates ----------

def _template(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _prompt(name: str, template: str) -> str:
    return (ROOT / "prompts" / name).read_text(encoding="utf-8").replace("{template}", template)


def closing_line(brief_template: str) -> str:
    """Last line of Part B's code block — fixed text, appended by code."""
    part_b = brief_template.split("## Part B", 1)[1]
    block = part_b.split("```", 2)[1]
    return [line for line in block.splitlines() if line.strip()][-1].strip()


def fixed_blocks(build_template: str) -> dict[str, str]:
    """Sections titled `## {NAME} — …` → {NAME: bullets}, inserted verbatim."""
    blocks = {}
    for m in re.finditer(r"^## \{([A-Z_]+)\}[^\n]*\n(.*?)(?=^## |\Z)", build_template, flags=re.S | re.M):
        lines = m.group(2).splitlines()
        start = next(i for i, line in enumerate(lines) if re.match(r"(- |\d+\. )", line))
        blocks[m.group(1)] = "\n".join(lines[start:]).strip()
    return blocks


# ---------- Rendering from state ----------

def _contact(state: State) -> dict:
    c = state.answers.get("contact")
    return c.value if c else {}


def stats(state: State, cfg: Config) -> dict:
    rows = engine.answers_view(state, cfg)
    return {
        "open_turns": state.turns_used,
        "follow_ups": sum(len(r["follow_ups"]) for r in rows),
        "skipped": len(state.skipped),
        "unanswered": sum(1 for r in rows if r["status"] != "answered"),
    }


def header(state: State, cfg: Config, today: date) -> str:
    name = state.answers["name"].value
    business = state.answers["business"].value
    c = _contact(state)
    contact = " · ".join(x for x in (name, c.get("phone"), c.get("email"), today.strftime("%d.%m.%Y")) if x)
    s = stats(state, cfg)
    return (f"# בריף אפיון: {business}\n\n{contact}\n"
            f"ראיון: {s['open_turns']} תורות · {s['follow_ups']} שאלות המשך · "
            f"{s['skipped']} דילוגים · {s['unanswered']} לא נענו")


def answers_markdown(state: State, cfg: Config) -> str:
    """Every question, answer and follow-up, in questionnaire order."""
    out, section = [], None
    for r in engine.answers_view(state, cfg):
        sec = cfg.by_id[r["id"]]["section"]
        if sec != section:
            section = sec
            title = next(s["title"] for s in cfg.sections if s["id"] == sec)
            out.append(f"\n### {title}\n")
        value = r["value"] if r["status"] == "answered" else "לא נענה"
        out.append(f"**{r['label']}**  \n{value}\n")
        for f in r["follow_ups"]:
            answer = f["answer"] if f["answer"] is not None else "(דילגו)"
            out.append(f"- *{f['question']}*  \n  {answer}")
        if r["follow_ups"]:
            out.append("")
    return "\n".join(out).strip()


# ---------- Run ----------

class Brief(BaseModel):
    """Step 1: what the client sees right away + the internal brief."""
    brief: str  # header + synthesis + appendix
    head: str
    body: str  # synthesis only — input for the build prompt
    client_summary: str
    scope_flags: list[ScopeFlag]
    open_items: list[str]


def write_brief(state: State, cfg: Config, synthesize: Synthesize, today: date | None = None) -> Brief:
    brief_template = _template("brief_template.md")
    answers = answers_markdown(state, cfg)
    syn = synthesize(_prompt("synthesis.md", brief_template), f"# הראיון\n\n{answers}")

    head = header(state, cfg, today or date.today())
    body = syn.brief_markdown.strip()
    closing = closing_line(brief_template)
    summary = syn.client_summary.strip()
    if closing not in summary:
        summary = f"{summary}\n\n{closing}"
    return Brief(brief=f"{head}\n\n{body}\n\n## נספח: תשובות מלאות\n\n{answers}\n", head=head, body=body,
                 client_summary=summary, scope_flags=syn.scope_flags, open_items=syn.open_items)


def write_build_prompt(brief: Brief, write: Write) -> str:
    """Step 2: build prompt from the brief (without the appendix)."""
    build_template = _template("build_prompt_template.md")
    build = write(_prompt("build.md", build_template), f"{brief.head}\n\n{brief.body}").strip()
    build = re.sub(r"^```(?:markdown|md)?\n(.*)\n```$", r"\1", build, flags=re.S)  # strip a wrapping fence
    for name, text in fixed_blocks(build_template).items():
        placeholder = "{" + name + "}"
        if placeholder in build:
            build = build.replace(placeholder, text)
        else:
            build = f"{build}\n\n## {name.replace('_', ' ').capitalize()}\n\n{text}"
    return build + "\n"


def run(state: State, cfg: Config, synthesize: Synthesize, write: Write, today: date | None = None) -> Result:
    b = write_brief(state, cfg, synthesize, today)
    return Result(brief=b.brief, client_summary=b.client_summary, build_prompt=write_build_prompt(b, write),
                  scope_flags=b.scope_flags, open_items=b.open_items)


def save(result: Result, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "brief.md").write_text(result.brief, encoding="utf-8")
    (out / "build_prompt.md").write_text(result.build_prompt, encoding="utf-8")
    (out / "client_summary.md").write_text(result.client_summary + "\n", encoding="utf-8")
    (out / "brief.json").write_text(
        result.model_dump_json(include={"scope_flags", "open_items"}, indent=2), encoding="utf-8")
