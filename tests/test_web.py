import asyncio

import pytest
import vercel.functions
from fastapi.testclient import TestClient

from briefai import web
from briefai.engine import Verdict
from briefai.synthesis import ScopeFlag, Synthesis

web._clients["judge"] = lambda system, exchange: Verdict(specific=False, reflection="הבנתי.", follow_up="אפשר דוגמה?")
web._clients["explain"] = lambda system, message: "הסבר"
web._clients["synthesis"] = (
    lambda system, user: Synthesis(brief_markdown="## בשורה אחת\nx", client_summary="תודה, דנה!",
                                   scope_flags=[ScopeFlag(item="a", detail="b")], open_items=[]),
    lambda system, user: "# Build\n{STANDARDS}",
)
sent_mail: list[dict] = []
web._clients["mail"] = lambda payload, key=None: sent_mail.append(payload)  # never a real email from tests
web.MIN_FILL_SECONDS = 0  # tests answer the form instantly; the bot tests set it back
client = TestClient(web.app)


class MemoryCache(dict):
    def set(self, key, value, options=None):
        self[key] = value


@pytest.fixture(autouse=True)
def fresh_usage(monkeypatch):
    """Each test starts with no LLM calls counted (today or per interview)."""
    monkeypatch.setitem(web._clients, "usage", MemoryCache())


def finished_interview(*names):
    v = client.post("/api/start").json()
    for value in names or ("דנה", "סטודיו", "dana@example.com"):
        v = step(v, type="reply", value=value)
    return finished_interview_from(v)


def finished_interview_from(v):
    while v["prompt"]["kind"] != "review":
        v = step(v, type="skip")
    return step(v, type="confirm")


def step(v, **action):
    r = client.post("/api/step", json={"state": v["state"], "sig": v["sig"], "action": action})
    assert r.status_code == 200, r.text
    return r.json()


def test_flow_and_signature():
    v = client.post("/api/start").json()
    assert v["prompt"]["kind"] == "form" and [f["id"] for f in v["form"]] == ["name", "business", "contact"]
    v = step(v, type="reply", value="דנה")
    v = step(v, type="reply", value="סטודיו")
    v = step(v, type="reply", value={"phone": "050-1234567", "email": ""})
    assert v["prompt"]["qid"] == "what"
    v = step(v, type="reply", value="אני מאמנת")
    assert v["prompt"]["kind"] == "follow_up" and v["prompt"]["reflection"] == "הבנתי."
    assert v["chat"][-1]["role"] == "client"

    # tampering with the state is rejected
    forged = {**v["state"], "turns_used": 0}
    r = client.post("/api/step", json={"state": forged, "sig": v["sig"], "action": {"type": "skip"}})
    assert r.status_code == 409

    # view round-trips
    again = client.post("/api/view", json={"state": v["state"], "sig": v["sig"]}).json()
    assert again["sig"] == v["sig"]


def test_finish_requires_confirmed_and_hides_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    v = client.post("/api/start").json()
    for value in ("דנה", "סטודיו", "dana@example.com"):
        v = step(v, type="reply", value=value)
    r = client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]})
    assert r.status_code == 400
    while v["prompt"]["kind"] != "review":
        v = step(v, type="skip")
    v = step(v, type="confirm")
    r = client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]})
    assert r.status_code == 200 and r.json() == {"ok": True}  # nothing from the brief goes to the browser
    saved = list((tmp_path / "transcripts").iterdir())[0]
    assert (saved / "build_prompt.md").exists() and (saved / "transcript.json").exists()
    mail = sent_mail[-1]
    assert mail["subject"] == "בריף אפיון: סטודיו — דנה" and mail["reply_to"] == "dana@example.com"
    assert [a["filename"] for a in mail["attachments"]][0] == "build_prompt.md"
    assert "# Build" in mail["text"]


def test_finish_keeps_answers_when_brief_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)

    def broken(system, user):
        raise RuntimeError("down")

    monkeypatch.setitem(web._clients, "synthesis", (broken, web._clients["synthesis"][1]))
    v = client.post("/api/start").json()
    for value in ("דנה", "סטודיו", "dana@example.com"):
        v = step(v, type="reply", value=value)
    while v["prompt"]["kind"] != "review":
        v = step(v, type="skip")
    v = step(v, type="confirm")
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).status_code == 200
    saved = list((tmp_path / "transcripts").iterdir())[0]
    assert (saved / "transcript.json").exists() and (saved / "error.txt").exists()
    mail = sent_mail[-1]  # the answers still reach Yasmin
    assert mail["subject"].startswith("⚠️") and [a["filename"] for a in mail["attachments"]] == ["transcript.json"]


def test_on_vercel_nothing_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setattr(web, "ON_VERCEL", True)
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "s" * 32)
    monkeypatch.setitem(client.headers, "x-briefai-key", "s" * 32)  # as the site's proxy sends it
    kept_alive = []  # wait_until: the work runs after the response
    monkeypatch.setattr(vercel.functions, "wait_until", kept_alive.append)
    v = finished_interview("דנה", "סטודיו", "vercel@example.com")
    before = len(sent_mail)
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).status_code == 200
    assert len(kept_alive) == 1
    asyncio.run(kept_alive[0])
    assert not (tmp_path / "transcripts").exists() and len(sent_mail) == before + 1


def test_finish_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    v = finished_interview("דנה", "סטודיו", "once@example.com")
    before = len(sent_mail)
    for _ in range(3):
        assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).json() == {"ok": True}
    assert len(sent_mail) == before + 1


def test_bots_get_through_but_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)

    def no_llm(*args):
        raise AssertionError("no LLM calls for bots")

    monkeypatch.setitem(web._clients, "judge", no_llm)
    before = len(sent_mail)

    # honeypot filled
    v = client.post("/api/start").json()
    v = step(v, type="reply", value="דנה", trap="https://spam.example")
    assert v["state"]["flagged"]
    v = step(v, type="reply", value="סטודיו")
    v = step(v, type="reply", value="bot@example.com")
    v = step(v, type="reply", value="אני מאמנת")  # open question: no judge
    assert v["prompt"]["kind"] != "follow_up"

    v = finished_interview_from(v)
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).json() == {"ok": True}
    assert len(sent_mail) == before and not (tmp_path / "transcripts").exists()


def test_form_sent_too_fast_asks_to_send_again(monkeypatch):
    monkeypatch.setattr(web, "MIN_FILL_SECONDS", 60)
    v = client.post("/api/start").json()
    v = step(v, type="reply", value="דנה")
    assert v["prompt"]["kind"] == "form" and v["prompt"]["notice"] == web.cfg.copy["welcome"]["too_fast"]
    assert "name" not in v["state"]["answers"] and not v["state"]["flagged"]
    monkeypatch.setattr(web, "MIN_FILL_SECONDS", 0)  # a moment later, sent again
    v = step(v, type="reply", value="דנה")
    assert v["state"]["answers"]["name"]["value"] == "דנה" and not v["prompt"]["notice"]


def test_email_shows_client_html_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    v = finished_interview('<a href="https://evil.example">לחצו</a>', "סטודיו ![](https://t.example/p.gif)",
                           "html@example.com")
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).status_code == 200
    body = sent_mail[-1]["html"]
    assert "<a " not in body and "<img" not in body and "&lt;a href" in body


def test_long_input_clipped():
    v = client.post("/api/start").json()
    v = step(v, type="reply", value="א" * 10000)
    assert len(v["state"]["answers"]["name"]["value"]) == 3000


def test_other_text_sent_with_choice():
    v = client.post("/api/start").json()
    for value in ("דנה", "סטודיו", "dana@example.com"):
        v = step(v, type="reply", value=value)
    while v["prompt"]["qid"] != "goals":
        v = step(v, type="skip")
    v = step(v, type="reply", value=["אחר"], other="לגייס מתנדבים")
    assert v["state"]["answers"]["goals"]["other"] == "לגייס מתנדבים"


def test_copy_and_form_ids():
    assert client.get("/api/copy").json()["welcome"]["start"]
    v = client.post("/api/start").json()
    assert v["form_ids"] == ["name", "business", "contact"]


def test_on_vercel_only_the_site_gets_in(monkeypatch):
    monkeypatch.setattr(web, "ON_VERCEL", True)
    monkeypatch.delenv("VERCEL_AUTOMATION_BYPASS_SECRET", raising=False)
    assert client.get("/api/copy", headers={"x-briefai-key": ""}).status_code == 404  # no secret set → closed
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "s" * 32)
    assert client.get("/api/copy").status_code == 404
    assert client.get("/api/copy", headers={"x-briefai-key": "wrong"}).status_code == 404
    assert client.get("/api/copy", headers={"x-briefai-key": "s" * 32}).status_code == 200


def test_demo_runs_but_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    before = len(sent_mail)
    v = client.post("/api/start", json={"demo": True}).json()
    assert v["state"]["demo"]
    for value in ("דנה", "סטודיו", "demo@example.com"):
        v = step(v, type="reply", value=value)
    v = step(v, type="reply", value="אני מאמנת")
    assert v["prompt"]["kind"] == "follow_up"  # the judge still runs in a demo
    v = finished_interview_from(v)
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).json() == {"ok": True}
    assert len(sent_mail) == before and not (tmp_path / "transcripts").exists()

    # a real interview can't be turned into a demo (or back): the state is signed
    real = client.post("/api/start", json={}).json()
    assert not real["state"]["demo"]
    r = client.post("/api/view", json={"state": {**real["state"], "demo": True}, "sig": real["sig"]})
    assert r.status_code == 409


def at_first_open_question():
    v = client.post("/api/start").json()
    for value in ("דנה", "סטודיו", "cap@example.com"):
        v = step(v, type="reply", value=value)
    assert v["prompt"]["qid"] == "what"
    return v


def counting_judge(monkeypatch):
    calls = []
    judge = web._clients["judge"]
    monkeypatch.setitem(web._clients, "judge", lambda *args: calls.append(1) or judge(*args))
    return calls


def test_replays_hit_the_interview_cap(monkeypatch):
    monkeypatch.setitem(web.cfg.settings, "max_llm_calls_per_interview", 3)
    calls = counting_judge(monkeypatch)
    v = at_first_open_question()
    # the same signed state, sent again and again: the turn budget in it resets, the cap doesn't
    kinds = [step(v, type="reply", value="אני מאמנת")["prompt"]["kind"] for _ in range(6)]
    assert len(calls) == 3 and kinds == ["follow_up"] * 3 + ["question"] * 3
    # another interview has its own cap
    assert step(at_first_open_question(), type="reply", value="אני מאמנת")["prompt"]["kind"] == "follow_up"
    assert len(calls) == 4


def test_daily_cap_keeps_the_interview_going(monkeypatch):
    monkeypatch.setitem(web.cfg.settings, "max_llm_calls_per_day", 1)
    calls = counting_judge(monkeypatch)
    assert step(at_first_open_question(), type="reply", value="אני מאמנת")["prompt"]["kind"] == "follow_up"
    v = step(at_first_open_question(), type="reply", value="אני מאמנת")  # over today's cap: no judge
    assert len(calls) == 1 and v["prompt"]["kind"] == "question" and v["prompt"]["qid"] != "what"
    assert v["state"]["answers"]["what"]["value"] == "אני מאמנת"


def test_over_daily_cap_finish_still_emails_the_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setitem(web.cfg.settings, "max_llm_calls_per_day", 0)

    def no_llm(*args):
        raise AssertionError("no synthesis over the daily cap")

    monkeypatch.setitem(web._clients, "synthesis", (no_llm, no_llm))
    v = finished_interview("דנה", "סטודיו", "overcap@example.com")
    before = len(sent_mail)
    assert client.post("/api/finish", json={"state": v["state"], "sig": v["sig"]}).json() == {"ok": True}
    assert len(sent_mail) == before + 1
    mail = sent_mail[-1]  # the raw answers, flagged as "no brief"
    assert mail["subject"].startswith("⚠️") and [a["filename"] for a in mail["attachments"]] == ["transcript.json"]
    assert (list((tmp_path / "transcripts").iterdir())[0] / "error.txt").exists()
