from dataclasses import replace

import pytest

from briefai import engine
from briefai.config import load
from briefai.engine import Confirm, Edit, Reply, Skip, Verdict


@pytest.fixture
def cfg():
    return load()


def vague_judge(system, exchange):
    return Verdict(specific=False, reflection="הבנתי שאתם מאמנים.", follow_up="אפשר דוגמה?")


def ok_judge(system, exchange):
    return Verdict(specific=True)


def clarify_judge(system, exchange):
    return Verdict(specific=False, clarify="הכוונה למה אתם מוכרים בפועל.")


def no_explain(system, message):
    raise AssertionError("explain should not be called")


def do(state, cfg, action, judge=ok_judge, explain=no_explain):
    return engine.submit(state, cfg, action, judge, explain)


def fill_form(cfg):
    s = engine.start()
    s = do(s, cfg, Reply(value="דנה"))
    s = do(s, cfg, Reply(value="סטודיו דנה"))
    s = do(s, cfg, Reply(value="050-1234567 dana@example.com"))
    return s


def advance_to(s, cfg, qid, judge=ok_judge):
    """Skip follow-ups and answer every question until `qid` is asked."""
    while (p := engine.next_prompt(s, cfg)).qid != qid or p.kind != "question":
        assert p.kind not in ("review", "done"), f"never reached {qid}"
        if p.kind == "follow_up":
            s = do(s, cfg, Skip(), judge)
        elif p.options:
            s = do(s, cfg, Reply(value=p.options[0]), judge)
        else:
            s = do(s, cfg, Reply(value="תשובה"), judge)
    return s


def test_form_first_and_not_skippable(cfg):
    s = engine.start()
    assert engine.next_prompt(s, cfg).qid == "name"
    s = do(s, cfg, Skip())
    assert engine.next_prompt(s, cfg).notice
    s = fill_form(cfg)
    assert s.answers["contact"].value == {"phone": "050-1234567", "email": "dana@example.com"}
    assert engine.next_prompt(s, cfg).qid == "what"


def test_contact_needs_phone_or_email(cfg):
    s = engine.start()
    s = do(s, cfg, Reply(value="דנה"))
    s = do(s, cfg, Reply(value="סטודיו"))
    s = do(s, cfg, Reply(value="אין לי"))
    assert engine.next_prompt(s, cfg).qid == "contact"
    assert s.notice


def test_skip_return_pass_then_unanswered(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Skip())  # what
    assert "what" in s.skipped
    # skip everything else
    while (p := engine.next_prompt(s, cfg)).kind != "review":
        if p.qid == "what":
            assert p.intro == engine.RETURN_INTRO
            s = do(s, cfg, Skip())
        else:
            s = do(s, cfg, Skip())
    assert "what" in s.unanswered
    assert s.turns_used == 0  # skips don't count
    s = do(s, cfg, Confirm())
    assert engine.next_prompt(s, cfg).kind == "done"


def test_judge_follow_ups_capped(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), vague_judge)
    assert engine.next_prompt(s, cfg).kind == "follow_up"
    s = do(s, cfg, Reply(value="פילאטיס"), vague_judge)
    s = do(s, cfg, Reply(value="מכשירים"), vague_judge)
    assert len(s.answers["what"].follow_ups) == 2
    assert engine.next_prompt(s, cfg).qid == "goals"


def test_skipping_judge_follow_up_stops_follow_ups(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), vague_judge)
    s = do(s, cfg, Skip(), vague_judge)
    assert engine.next_prompt(s, cfg).qid == "goals"


def test_choice_follow_up_key_asked_once(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["שיקבעו תור לבד"]))
    p = engine.next_prompt(s, cfg)
    assert p.kind == "follow_up" and "תורים" in p.text
    s = do(s, cfg, Reply(value="פגישות ייעוץ, ב-Calendly"))
    s = advance_to(s, cfg, "features")
    s = do(s, cfg, Reply(value=["קביעת תורים"]))
    assert engine.next_prompt(s, cfg).qid == "links"  # booking already asked


def test_choice_follow_ups_max_per_question(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["למכור מוצרים באתר", "שיקבעו תור לבד", "לתת מידע ולחסוך שאלות חוזרות"]))
    assert len([p for p in s.pending if p.source == "choice"]) == 2


def test_skip_if_asked(cfg):
    s = advance_to(fill_form(cfg), cfg, "features")
    s = do(s, cfg, Reply(value=["כמה שפות"]))
    assert engine.next_prompt(s, cfg).qid == "links"  # languages question covers it


def test_other_text_comes_with_the_choice(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["אחר"]))  # no text → stay, ask for it
    assert "goals" not in s.answers and s.notice == engine.OTHER_MISSING
    assert engine.next_prompt(s, cfg).qid == "goals"
    s = do(s, cfg, Reply(value=["אחר"], other="לגייס מתנדבים"))
    assert engine.display(cfg, "goals", s.answers["goals"]) == "אחר: לגייס מתנדבים"
    assert engine.next_prompt(s, cfg).qid != "goals"  # no separate "מה בדיוק?" prompt
    assert s.transcript[-1].text == "אחר: לגייס מתנדבים"


def test_edit_away_from_other_clears_text(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["אחר"], other="לגייס מתנדבים"))
    s = do(s, cfg, Edit(target="goals"))
    assert engine.next_prompt(s, cfg).other == "לגייס מתנדבים"
    s = do(s, cfg, Reply(value=["שיבינו שאפשר לסמוך עליי"], other="ישן"))
    assert s.answers["goals"].other is None


def test_judge_clarify_stays_on_question(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="מה זה אומר?"), clarify_judge)
    assert "what" not in s.answers and s.turns_used == 0
    p = engine.next_prompt(s, cfg)
    assert p.qid == "what" and p.kind == "question"
    assert p.explanation == "הכוונה למה אתם מוכרים בפועל."
    s = do(s, cfg, Reply(value="סטודיו לפילאטיס"))
    assert s.answers["what"].value == "סטודיו לפילאטיס"
    assert engine.next_prompt(s, cfg).explanation is None


def test_judge_clarify_on_follow_up_keeps_it_pending(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), vague_judge)
    used = s.turns_used
    s = do(s, cfg, Reply(value="לא הבנתי"), clarify_judge)
    p = engine.next_prompt(s, cfg)
    assert p.kind == "follow_up" and p.explanation and s.turns_used == used
    assert not s.answers["what"].follow_ups


def test_reflection_opens_judge_follow_up(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), vague_judge)
    p = engine.next_prompt(s, cfg)
    assert p.reflection == "הבנתי שאתם מאמנים." and p.text == "אפשר דוגמה?"
    s = do(s, cfg, Reply(value="פילאטיס"))
    f = s.answers["what"].follow_ups[0]
    assert f.question == "אפשר דוגמה?" and f.reflection == "הבנתי שאתם מאמנים."


def test_choice_follow_up_clarification_uses_explainer(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["שיקבעו תור לבד"]))
    s = do(s, cfg, Reply(value="מה זה אומר?"), explain=lambda system, m: "רוצים לדעת איך קובעים אצלכם.")
    p = engine.next_prompt(s, cfg)
    assert p.kind == "follow_up" and p.explanation == "רוצים לדעת איך קובעים אצלכם."
    assert not s.answers["goals"].follow_ups


def test_explainer_can_say_it_was_an_answer(cfg):
    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["שיקבעו תור לבד"]))
    s = do(s, cfg, Reply(value="אולי דרך וואטסאפ?"), explain=lambda system, m: None)
    assert s.answers["goals"].follow_ups[0].answer == "אולי דרך וואטסאפ?"


def test_explainer_failure_falls_back_only_for_clear_requests(cfg):
    def broken(system, message):
        raise RuntimeError("down")

    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["שיקבעו תור לבד"]))
    t = do(s, cfg, Reply(value="לא הבנתי"), explain=broken)
    assert engine.next_prompt(t, cfg).explanation == engine.EXPLAIN_FALLBACK
    t = do(s, cfg, Reply(value="וואטסאפ?"), explain=broken)
    assert t.answers["goals"].follow_ups[0].answer == "וואטסאפ?"


def test_judge_failure_then_heuristic(cfg):
    def broken(system, exchange):
        raise RuntimeError("down")

    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="מה הכוונה?"), broken, explain=lambda system, m: "הסבר")
    assert "what" not in s.answers and engine.next_prompt(s, cfg).explanation == "הסבר"


def test_edit_choice_reevaluates_show_if_and_follow_ups(cfg):
    s = advance_to(fill_form(cfg), cfg, "domain")
    s = do(s, cfg, Reply(value="אין עדיין"))
    s = do(s, cfg, Reply(value="danastudio"))  # domain_idea follow-up
    assert engine.next_prompt(s, cfg).kind == "review"

    s = do(s, cfg, Edit(target="domain"))
    assert engine.next_prompt(s, cfg).editing == "domain"
    s = do(s, cfg, Reply(value="יש"))
    assert s.answers["domain"].follow_ups[0].dropped
    assert engine.next_prompt(s, cfg).qid == "domainName"  # newly relevant


def test_edit_open_answer_no_judge_no_budget(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="סטודיו לפילאטיס"))
    used = s.turns_used
    s = do(s, cfg, Edit(target="what"))
    s = do(s, cfg, Reply(value="סטודיו לפילאטיס מכשירים"), vague_judge)
    assert s.answers["what"].value == "סטודיו לפילאטיס מכשירים"
    assert s.turns_used == used and not s.pending


def test_edit_skipped_question_answers_it(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Skip())  # what
    s = do(s, cfg, Edit(target="what"))
    assert engine.next_prompt(s, cfg).current is None
    s = do(s, cfg, Reply(value="סטודיו"), vague_judge)
    assert "what" in s.answers
    assert engine.next_prompt(s, cfg).kind == "follow_up"  # first answer → judged
    assert s.turns_used == 1
    assert "what" not in [r["id"] for r in engine.answers_view(s, cfg) if r["status"] != "answered"]


def test_edit_follow_up(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), vague_judge)
    s = do(s, cfg, Reply(value="פילאטיס"))
    s = do(s, cfg, Edit(target="what.1"))
    s = do(s, cfg, Reply(value="פילאטיס מכשירים"))
    assert s.answers["what"].follow_ups[0].answer == "פילאטיס מכשירים"


def test_cannot_edit_unreached(cfg):
    s = fill_form(cfg)
    s = do(s, cfg, Edit(target="extra"))
    assert s.notice and s.editing is None


def test_budget_exhausted_no_follow_ups(cfg):
    small = replace(cfg, settings={**cfg.settings, "max_turns": 1})
    s = fill_form(small)
    s = do(s, small, Reply(value="אני מאמנת"), vague_judge)  # uses the only turn
    assert not s.pending
    s = do(s, small, Reply(value=["למכור מוצרים באתר"]))
    assert not s.pending  # no choice follow-ups either
    assert engine.next_prompt(s, small).qid == "customer"  # questions still asked


def test_supplementary_off(cfg):
    core = replace(cfg, settings={**cfg.settings, "include_supplementary": False})
    assert "links" not in core.chat_ids and "what" in core.chat_ids


def test_judge_failure_does_not_block(cfg):
    def broken(system, exchange):
        raise RuntimeError("down")

    s = fill_form(cfg)
    s = do(s, cfg, Reply(value="אני מאמנת"), broken)
    assert engine.next_prompt(s, cfg).qid == "goals"


def test_state_round_trips_as_json(cfg):
    s = advance_to(fill_form(cfg), cfg, "style")
    again = engine.State.model_validate_json(s.model_dump_json())
    assert again == s


def test_none_option_is_an_answer(cfg):
    s = advance_to(fill_form(cfg), cfg, "features")
    p = engine.next_prompt(s, cfg)
    assert p.options[-1] == "לא, תודה" and "לא, תודה" in p.exclusive
    s = do(s, cfg, Reply(value=["לא, תודה"]))
    assert s.answers["features"].value == ["לא, תודה"] and "features" not in s.skipped
    assert engine.next_prompt(s, cfg).qid == "links"


def test_none_option_dropped_when_combined(cfg):
    s = advance_to(fill_form(cfg), cfg, "features")
    s = do(s, cfg, Reply(value=["לא, תודה", "חנות"]))
    assert s.answers["features"].value == ["חנות"]


def test_list_question_hint(cfg):
    assert cfg.hint("inspiration").startswith("קישור לאתר, ומה אהבתם בו? למשל:")


def test_progress(cfg):
    s = fill_form(cfg)
    prog = engine.progress(s, cfg)
    assert [x["id"] for x in prog] == ["about", "goals", "design", "content"]
    assert prog[0]["current"] and not prog[1]["current"]
    s = do(s, cfg, Reply(value="סטודיו"))  # what → goals section
    prog = engine.progress(s, cfg)
    assert prog[0]["reached"] == prog[0]["total"] and prog[1]["current"]


def test_copy_covers_questions(cfg):
    open_ids = {q["id"] for q in cfg.questions if cfg.kind(q["id"]) == "open"}
    assert set(cfg.copy["questions"]) == open_ids
    assert set(cfg.copy["topics"]) == {s["id"] for s in cfg.sections}
    review = cfg.copy["review"]
    listed = review["header"] + [q for sec in review["sections"] for q in sec["questions"]]
    assert sorted(listed) == sorted(cfg.by_id)  # every question exactly once


def test_open_none_answer_not_judged_not_skipped(cfg):
    s = advance_to(fill_form(cfg), cfg, "inspiration")
    p = engine.next_prompt(s, cfg)
    assert p.none == "אין לי אתר כזה" and not p.options
    used = s.turns_used
    s = do(s, cfg, Reply(value=p.none), vague_judge)
    assert s.answers["inspiration"].value == "אין לי אתר כזה"
    assert not s.pending and s.turns_used == used and "inspiration" not in s.skipped


def test_judge_sees_other_answers_follow_ups(cfg):
    seen = []

    def spy(system, exchange):
        seen.append(system)
        return Verdict(specific=True)

    s = advance_to(fill_form(cfg), cfg, "goals")
    s = do(s, cfg, Reply(value=["שיקבעו תור לבד"]))
    s = do(s, cfg, Reply(value="אימון אישי, ב-Calendly"))
    s = do(s, cfg, Reply(value="נשים אחרי לידה"), spy)  # customer → judged
    assert "→ אימון אישי, ב-Calendly" in seen[-1]


def test_colors_answer(cfg):
    s = advance_to(fill_form(cfg), cfg, "colorsStatus")
    s = do(s, cfg, Reply(value="יש צבעי מותג מוגדרים"))
    p = engine.next_prompt(s, cfg)
    assert p.qid == "colors" and p.input == "colors"
    s = do(s, cfg, Reply(value=["טורקיז (#1aa6a6)", "#ffffff", "#ffffff", "a", "b", "c"]))
    assert s.answers["colors"].value == ["טורקיז (#1aa6a6)", "#ffffff", "a", "b"]  # deduped, max 4
    assert not s.pending  # choice kind: never judged


def test_list_answer_items(cfg):
    s = advance_to(fill_form(cfg), cfg, "inspiration")
    p = engine.next_prompt(s, cfg)
    assert [f["key"] for f in p.fields] == ["url", "note"]
    s = do(s, cfg, Reply(value=[{"url": "aesop.com", "note": "השקט"}, {"url": "", "note": ""}, {"url": "x.com"}]))
    assert s.answers["inspiration"].value == [{"url": "aesop.com", "note": "השקט"}, {"url": "x.com", "note": ""}]
    assert engine.display(cfg, "inspiration", s.answers["inspiration"]) == "aesop.com — השקט\nx.com"


def test_style_previews(cfg):
    s = advance_to(fill_form(cfg), cfg, "style")
    p = engine.next_prompt(s, cfg)
    assert p.previews[:4] == ["minimal", "warm", "bold", "elegant"] and p.previews[4] == ""
