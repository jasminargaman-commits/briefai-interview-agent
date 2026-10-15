from datetime import date

import pytest

from briefai import engine, synthesis
from briefai.config import load
from briefai.engine import Confirm, Reply, Skip, Verdict
from briefai.synthesis import ScopeFlag, Synthesis


@pytest.fixture
def cfg():
    return load()


def ok_judge(system, exchange):
    return Verdict(specific=True)


def finished_interview(cfg):
    s = engine.start()
    for v in ("דנה", "סטודיו דנה", "050-1234567 dana@example.com"):
        s = engine.submit(s, cfg, Reply(value=v), ok_judge)
    s = engine.submit(s, cfg, Reply(value="סטודיו לפילאטיס"), ok_judge)  # what
    while engine.next_prompt(s, cfg).kind != "review":
        s = engine.submit(s, cfg, Skip(), ok_judge)  # skip twice → unanswered
    return engine.submit(s, cfg, Confirm(), ok_judge)


def fake_synthesize(system, user):
    assert "סטודיו לפילאטיס" in user and "{template}" not in system
    return Synthesis(brief_markdown="## בשורה אחת\nסטודיו.", client_summary="תודה, דנה! הנה מה שהבנתי:\n- סטודיו",
                     scope_flags=[ScopeFlag(item="תורים", detail="רוצים הזמנה עצמית")], open_items=["למי האתר"])


def fake_write(system, user):
    assert "# בריף אפיון: סטודיו דנה" in user and "נספח" not in user
    return ("```markdown\n# Build: סטודיו דנה website\n\n## Ground rules\n{GROUND_RULES}\n\n"
            "## Requirements\n\n{STANDARDS}\n```")


def test_full_run(cfg):
    s = finished_interview(cfg)
    r = synthesis.run(s, cfg, fake_synthesize, fake_write, today=date(2026, 10, 5))

    assert r.brief.startswith("# בריף אפיון: סטודיו דנה\n\nדנה · 050-1234567 · dana@example.com · 05.10.2026")
    assert "## בשורה אחת" in r.brief
    assert "## נספח: תשובות מלאות" in r.brief and "לא נענה" in r.brief

    assert r.client_summary.endswith("תוך 2 ימי עסקים.")  # fixed closing line added by code

    assert not r.build_prompt.startswith("```")
    assert "{STANDARDS}" not in r.build_prompt and "Hosting: Vercel" in r.build_prompt
    assert "{GROUND_RULES}" not in r.build_prompt and "When something is unclear, ask" in r.build_prompt
    assert "## How to work" in r.build_prompt and "Lighthouse" in r.build_prompt  # missing placeholder → appended
    assert r.scope_flags[0].item == "תורים"


def test_stats(cfg):
    s = finished_interview(cfg)
    st = synthesis.stats(s, cfg)
    assert st["open_turns"] == 1 and st["unanswered"] == st["skipped"] > 0


def test_templates_parse():
    assert synthesis.closing_line(synthesis._template("brief_template.md")).startswith("תודה רבה")
    blocks = synthesis.fixed_blocks(synthesis._template("build_prompt_template.md"))
    assert set(blocks) == {"GROUND_RULES", "HOW_TO_WORK", "STANDARDS"}
    assert blocks["STANDARDS"].startswith("- Hebrew, RTL")
    assert blocks["HOW_TO_WORK"].startswith("1. Read this whole prompt")
