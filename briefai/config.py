"""Loads the interview config files. The engine knows nothing about the
brief questionnaire itself — everything specific lives in these files."""

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHOICE_TYPES = {"choice", "cards", "multi", "scale", "colors"}
OTHER = "אחר"


@dataclass(frozen=True)
class Config:
    sections: list[dict]
    questions: list[dict]  # flat, in questionnaire order
    by_id: dict[str, dict]
    intents: dict
    settings: dict
    judge_prompt: str
    explain_prompt: str
    copy: dict  # fixed UI texts (copy.json)

    def kind(self, qid: str) -> str:
        """form (collected before the chat), choice (fixed input, never
        judged) or open (asked conversationally, may be judged)."""
        q = self.by_id[qid]
        if q.get("required") or q["type"] == "contact":
            return "form"
        if q["type"] in CHOICE_TYPES:
            return "choice"
        return "open"

    def options(self, qid: str) -> list[str]:
        q = self.by_id[qid]
        values = [o if isinstance(o, str) else o["value"] for o in q.get("options", [])]
        if q.get("other"):
            values.append(OTHER)
        if self.none_option(qid) and self.kind(qid) == "choice":
            values.append(self.none_option(qid))
        return values

    def none_option(self, qid: str) -> str | None:
        """'None' answer (intents.json → none_options): an extra option for a
        multi question, a button for an open one."""
        return self.intents.get("none_options", {}).get(qid)

    def exclusive(self, qid: str) -> list[str]:
        """Options that can't be combined with others."""
        none = self.none_option(qid)
        return self.by_id[qid].get("exclusive", []) + ([none] if none else [])

    def hint(self, qid: str) -> str | None:
        q = self.by_id[qid]
        if q.get("help") or q.get("placeholder"):
            return q.get("help") or q.get("placeholder")
        if q["type"] == "list":  # e.g. "קישור לאתר, ומה אהבתם בו? למשל: …"
            fields = q.get("fields", [])
            labels = [f["label"] for f in fields]
            text = ", ".join([labels[0]] + ["ו" + label for label in labels[1:]]) if labels else ""
            example = next((f["placeholder"] for f in fields if f.get("placeholder", "").startswith("למשל")), "")
            return f"{text} {example}".strip() or None
        return None

    def included(self, qid: str) -> bool:
        """Part of this interview at all (before showIf)."""
        q = self.by_id[qid]
        return q["status"] == "core" or self.settings["include_supplementary"]

    def judged(self, qid: str) -> bool:
        return self.kind(qid) == "open" and qid in self.intents["questions"]

    def choice_follow_ups(self, qid: str) -> dict:
        return self.intents.get("choice_follow_ups", {}).get(qid, {})

    @property
    def form_ids(self) -> list[str]:
        return [q["id"] for q in self.questions if self.kind(q["id"]) == "form"]

    @property
    def chat_ids(self) -> list[str]:
        return [q["id"] for q in self.questions if self.kind(q["id"]) != "form" and self.included(q["id"])]


def load(root: Path = ROOT) -> Config:
    data = json.loads((root / "questions.json").read_text(encoding="utf-8"))
    sections = data["sections"]
    questions = [q for s in sections for q in s["questions"]]
    return Config(
        sections=sections,
        questions=questions,
        by_id={q["id"]: q for q in questions},
        intents=json.loads((root / "intents.json").read_text(encoding="utf-8")),
        settings=json.loads((root / "settings.json").read_text(encoding="utf-8")),
        judge_prompt=(root / "prompts" / "judge.md").read_text(encoding="utf-8"),
        explain_prompt=(root / "prompts" / "explain.md").read_text(encoding="utf-8"),
        copy=json.loads((root / "copy.json").read_text(encoding="utf-8")),
    )
