"""CLI: runs the interview in the terminal, prints the raw transcript, then
writes the brief. Holds the state in a local variable; the engine is pure."""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from . import engine
from .config import OTHER, ROOT, Config, load
from .engine import Confirm, Edit, Reply, Skip, State

HELP = """פקודות:
  /skip  (או: דלג)   דילוג על השאלה
  /answers          כל התשובות עד עכשיו
  /skipped          שאלות שדילגתם עליהן
  /edit <id>        עריכת תשובה (למשל: /edit what, או /edit what.1 לשאלת המשך)
  /quit             שמירה ויציאה
  /help             העזרה הזו"""


def print_answers(state: State, cfg: Config) -> None:
    rows = engine.answers_view(state, cfg)
    if not rows:
        print("  (עוד אין תשובות)")
    for r in rows:
        value = {"skipped": "(דילגתם)", "unanswered": "(לא נענה)"}.get(r["status"], r["value"])
        print(f"  [{r['id']}] {r['label']}\n      {value}")
        for f in r["follow_ups"]:
            answer = f["answer"] if f["answer"] is not None else "(דילגתם)"
            print(f"      [{r['id']}.{f['n']}] {f['question']}\n          {answer}")


def print_skipped(state: State, cfg: Config) -> None:
    rows = [r for r in engine.answers_view(state, cfg) if r["status"] != "answered"]
    if not rows:
        print("  (אין שאלות שדילגתם עליהן)")
    for r in rows:
        print(f"  [{r['id']}] {r['label']}")


def print_transcript(state: State) -> None:
    print("\n" + "=" * 20 + " תמליל " + "=" * 20)
    for e in state.transcript:
        who = {"agent": "סוכן", "client": "לקוח", "system": "  ·"}[e.role]
        print(f"{who}: {e.text}")
    print(f"\nתורות שנוצלו: {state.turns_used}")


def save(state: State, cfg: Config) -> Path:
    business = state.answers.get("business")
    name = re.sub(r"[^\w\-]+", "-", str(business.value)).strip("-") if business else "draft"
    out = ROOT / "transcripts" / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return out


def render(p: engine.Prompt) -> None:
    print()
    if p.notice:
        print(f"! {p.notice}")
    if p.intro:
        print(p.intro + "\n")
    if p.editing:
        print(f"(עריכה — התשובה הנוכחית: {p.current}. /skip לביטול)" if p.current
              else "(חוזרים לשאלה שדילגתם עליה. /skip לביטול)")
    if p.explanation:
        print(p.explanation + "\n")
    print(f"› {p.reflection} {p.text}" if p.reflection else f"› {p.text}")
    if p.hint:
        print(f"  {p.hint}")
    if p.none:
        print(f"  (אם אין — כתבו: {p.none})")
    for i, (opt, hint) in enumerate(zip(p.options, p.option_hints or [""] * len(p.options), strict=False), 1):
        print(f"   {i}. {opt}" + (f" — {hint}" if hint else ""))
    if p.options:
        print("  " + ("אפשר לבחור כמה: מספרים מופרדים בפסיק" if p.multi else "בחרו מספר"))


def to_value(p: engine.Prompt, text: str):
    """Numbers → option values. Anything else is passed as-is for the engine to validate."""
    if not p.options:
        return text
    parts = [x for x in re.split(r"[,\s]+", text) if x]
    if parts and all(x.isdigit() for x in parts):
        values = [p.options[int(x) - 1] if 1 <= int(x) <= len(p.options) else x for x in parts]
        return values if p.multi else values[0] if len(values) == 1 else values
    return [x.strip() for x in text.split(",")] if p.multi else text


def ask_other(p: engine.Prompt, value) -> str | None:
    """"אחר" is answered together with the choice, like the inline field on the web."""
    chosen = value if isinstance(value, list) else [value]
    if OTHER not in chosen:
        return None
    default = f" [{p.other}]" if p.other else ""
    try:
        text = input(f"  {OTHER} — {engine.OTHER_MISSING}{default}\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return text or p.other


def run(state: State, cfg: Config) -> None:
    judge = explain = None
    print("BriefAI — ראיון (CLI). /help לפקודות.")
    while True:
        p = engine.next_prompt(state, cfg)
        if p.kind == "done":
            break
        if p.kind == "review":
            print("\n" + "-" * 50)
            print_answers(state, cfg)
            print(f"\n› {p.text}")
            print("  Enter לאישור, או /edit <id>")
        else:
            render(p)

        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\nנשמר: {save(state, cfg)}")
            return

        cmd, _, arg = text.partition(" ")
        if cmd == "/help":
            print(HELP)
            continue
        if cmd == "/answers":
            print_answers(state, cfg)
            continue
        if cmd == "/skipped":
            print_skipped(state, cfg)
            continue
        if cmd == "/quit":
            print(f"נשמר: {save(state, cfg)}")
            return

        if cmd == "/edit":
            action = Edit(target=arg.strip())
        elif cmd in ("/skip", "דלג"):
            action = Skip()
        elif p.kind == "review" and not text:
            action = Confirm()
        else:
            value = to_value(p, text)
            action = Reply(value=value, other=ask_other(p, value) if p.options else None)

        if judge is None and isinstance(action, Reply):
            from .llm import make_explain, make_judge
            judge, explain = make_judge(), make_explain()
        state = engine.submit(state, cfg, action, judge, explain)

    print_transcript(state)
    path = save(state, cfg)
    print(f"\nנשמר: {path}")
    synthesize_file(path, cfg)


def synthesize_file(path: Path, cfg: Config, email: bool = False) -> None:
    """Brief, client summary and build prompt from a saved interview."""
    from . import synthesis
    from .llm import make_synthesis

    state = State.model_validate_json(path.read_text(encoding="utf-8"))
    if not state.confirmed:
        sys.exit("הראיון הזה לא הושלם (לא אושר בסוף) — אפשר להמשיך אותו עם --resume.")
    print("\nמסכמת...")
    result = synthesis.run(state, cfg, *make_synthesis())

    out = path.with_suffix("")
    synthesis.save(result, out)

    print("\n" + "=" * 20 + " סיכום ללקוח " + "=" * 20)
    print(result.client_summary)
    print(f"\nהבריף, פרומפט הבנייה וה-JSON נשמרו ב: {out}/")

    if email:
        from . import mail

        send = mail.make_sender()
        if not send:
            sys.exit("חסרים RESEND_API_KEY / MAIL_FROM / MAIL_TO ב-.env — המייל לא נשלח.")
        send(mail.compose(state, cfg, result))
        print("המייל נשלח.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="briefai")
    ap.add_argument("--resume", type=Path, help="continue from a saved transcript JSON")
    ap.add_argument("--synthesize", type=Path, help="write the brief from a finished transcript JSON")
    ap.add_argument("--email", action="store_true", help="with --synthesize: also email the brief (Resend)")
    args = ap.parse_args(argv)
    if args.synthesize:
        synthesize_file(args.synthesize, load(), email=args.email)
        return
    state = State.model_validate_json(args.resume.read_text(encoding="utf-8")) if args.resume else engine.start()
    run(state, load())


if __name__ == "__main__":
    main(sys.argv[1:])
