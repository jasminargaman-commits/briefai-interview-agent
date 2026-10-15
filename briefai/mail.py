"""The finished interview is emailed to Yasmin via Resend. Body: brief + answers appendix + build prompt;
build_prompt.md attached. If the brief failed, the raw answers still go out —
an interview is never lost to an LLM error."""

import base64
import html
import json
import os
from collections.abc import Callable

import httpx
import markdown
from markdown.extensions import Extension

from . import synthesis
from .config import Config
from .engine import State

RESEND_URL = "https://api.resend.com/emails"

# (Resend payload without from/to, idempotency key or None) -> None. Raises on failure.
Send = Callable[..., None]


def make_sender() -> Send | None:
    """None when RESEND_API_KEY / MAIL_FROM / MAIL_TO aren't all set (e.g. local
    testing without email)."""
    key, sender, to = (os.environ.get(k) for k in ("RESEND_API_KEY", "MAIL_FROM", "MAIL_TO"))
    if not (key and sender and to):
        return None

    def send(payload: dict, idempotency_key: str | None = None) -> None:
        # Same key within 24h → Resend sends one email, not two.
        headers = {"Authorization": f"Bearer {key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        r = httpx.post(RESEND_URL, headers=headers, timeout=20,
                       json={"from": sender, "to": [a.strip() for a in to.split(",")], **payload})
        r.raise_for_status()

    return send


class _TextOnly(Extension):
    """Client answers (and the brief built from them) are rendered without raw
    HTML, links or images — whatever a client types shows as text, never as a
    clickable link or a remote image in Yasmin's inbox."""

    def extendMarkdown(self, md):
        md.preprocessors.deregister("html_block")
        for name in ("html", "link", "image_link", "image_reference", "reference",
                     "short_reference", "short_image_ref", "autolink", "automail"):
            if name in md.inlinePatterns:
                md.inlinePatterns.deregister(name)


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=[_TextOnly()])


def _attachment(filename: str, text: str) -> dict:
    return {"filename": filename, "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}


def _html(sections: list[str]) -> str:
    return ('<div dir="rtl" style="font-family:Arial,sans-serif;line-height:1.6;max-width:760px">'
            + "<hr>".join(sections) + "</div>")


def compose(state: State, cfg: Config, result: synthesis.Result | None, error: str | None = None) -> dict:
    """The email for one finished interview. `result` is None when the brief failed."""
    name = state.answers["name"].value
    business = state.answers["business"].value
    email = (state.answers["contact"].value or {}).get("email")
    transcript = _attachment("transcript.json", state.model_dump_json(indent=2))

    if result is None:
        answers = synthesis.answers_markdown(state, cfg)
        note = f"הבריף לא נכתב ({error or 'שגיאה'}). אלה התשובות המלאות — אפשר להריץ שוב עם --synthesize."
        payload = {
            "subject": f"⚠️ אפיון חדש (בלי בריף): {business} — {name}",
            "text": f"{note}\n\n{answers}",
            "html": _html([f"<p><strong>{html.escape(note)}</strong></p>", _md(answers)]),
            "attachments": [transcript],
        }
    else:
        build = result.build_prompt
        flags = json.dumps(result.model_dump(include={"scope_flags", "open_items"}), ensure_ascii=False, indent=2)
        payload = {
            "subject": f"בריף אפיון: {business} — {name}",
            "text": f"{result.brief}\n\n---\n\n# פרומפט בנייה\n\n{build}",
            "html": _html([
                _md(result.brief),
                "<h2>פרומפט בנייה</h2>"
                f'<pre style="white-space:pre-wrap;unicode-bidi:plaintext;font-size:13px">{html.escape(build)}</pre>',
            ]),
            "attachments": [_attachment("build_prompt.md", build), _attachment("brief.md", result.brief),
                            _attachment("brief.json", flags), transcript],
        }
    if email:
        payload["reply_to"] = email
    return payload
