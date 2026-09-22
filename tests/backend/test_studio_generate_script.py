"""Tests for the Studio first-draft-via-production-engine work.

Covers:
  C  script_packaging_service.build_viral_script_block  (shared viral block)
  A  short-video FIRST draft routed through llm.generate_script
     + _short_first_draft_topic extraction + MB_STUDIO_GENERATE_SCRIPT env
  B  _local_agent_fallback is_fallback flag + honest holding reply
     + fallback output never becomes candidate_script
"""
import json
from unittest.mock import patch

import pytest


@pytest.fixture
def db():
    from backend.database import Base, SessionLocal, engine
    import backend.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    backend.models.apply_lightweight_migrations()
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset(db):
    from backend.models.studio_session import StudioSession
    db.query(StudioSession).delete()
    db.commit()
    from backend.lib.agent_safety import _msg_log
    _msg_log.clear()
    yield


# ----------------------------------------------------------------------
# C — shared viral packaging block
# ----------------------------------------------------------------------









# ----------------------------------------------------------------------
# A — topic extraction + env toggle
# ----------------------------------------------------------------------

def test_generate_script_enabled_default_and_off(monkeypatch):
    from backend.services.studio_agent import _studio_generate_script_enabled
    monkeypatch.delenv("MB_STUDIO_GENERATE_SCRIPT", raising=False)
    assert _studio_generate_script_enabled() is True
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "0")
    assert _studio_generate_script_enabled() is False
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "off")
    assert _studio_generate_script_enabled() is False


def test_short_first_draft_topic_from_spec():
    from backend.services.studio_agent import _short_first_draft_topic
    topic = _short_first_draft_topic([], "随便", {"topic": "马斯克的火星执念"})
    assert "火星" in topic


def test_short_first_draft_topic_ignores_internal_codes():
    from backend.services.studio_agent import _short_first_draft_topic
    # direction/video_type hold internal codes and must NOT be used as topic
    topic = _short_first_draft_topic(
        [], "嗯", {"direction": "knowledge", "video_type": "tutorial"},
    )
    assert topic == ""
    assert "knowledge" not in topic


def test_short_first_draft_topic_insufficient_returns_empty():
    from backend.services.studio_agent import _short_first_draft_topic
    # bare greeting / category-only → no topic (caller keeps 追问)
    assert _short_first_draft_topic([], "你好", {}) == ""


# ----------------------------------------------------------------------
# A — end-to-end: short-video first draft uses generate_script
# ----------------------------------------------------------------------

_DRAFT_JSON = json.dumps({
    "say": "[第 1 稿 · 约 80 字 ≈ 60 秒]\n\n这是对话模型写的草稿。第一句。第二句在此。",
    "spec_updates": {},
    "tool_call": None,
})


def _short_session(db, **spec_extra):
    from backend.models.studio_session import StudioSession
    spec = {
        "platform": "youtube_shorts",
        "aspect_ratio": "9:16",
        "output_format": "youtube_shorts",
        "duration_seconds": 60,
        "count": 1,
        "voice": "azure_yunyang",
        "voice_speed": 1.0,
    }
    spec.update(spec_extra)
    sess = StudioSession(spec=spec)
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess




def test_short_first_draft_env_off_keeps_conversational(db, monkeypatch):
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "0")
    from backend.services.studio_agent import StudioAgent

    sess = _short_session(db)
    with patch("backend.lib.llm_client.LLMClient._call", return_value=_DRAFT_JSON), \
         patch("backend.lib.llm_client.LLMClient.generate_script") as mgen:
        result = StudioAgent().step(db, sess, "做一条关于章鱼的短视频")

    mgen.assert_not_called()
    assert "对话模型写的草稿" in result["assistant_text"]


def test_short_first_draft_never_overwrites_user_pasted_script(db, monkeypatch):
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "1")
    from backend.services.studio_agent import StudioAgent

    # candidate_source == "user" → generate_script must NOT run
    sess = _short_session(
        db,
        candidate_script="用户自己贴的完整脚本正文。第一句。第二句。第三句结尾。",
        candidate_source="user",
    )
    with patch("backend.lib.llm_client.LLMClient._call", return_value=_DRAFT_JSON), \
         patch("backend.lib.llm_client.LLMClient.generate_script") as mgen:
        StudioAgent().step(db, sess, "做一条关于章鱼的短视频")

    mgen.assert_not_called()


# ----------------------------------------------------------------------
# B — fallback hardening
# ----------------------------------------------------------------------

def test_local_fallback_flags_is_fallback_and_is_honest():
    from backend.services.studio_agent import _local_agent_fallback
    from backend.services.studio_flow import looks_like_ai_draft

    spec = {
        "video_type": "tutorial",
        "direction": "knowledge",
        "platform": "youtube_shorts",
        "aspect_ratio": "9:16",
        "duration_seconds": 60,
        "count": 1,
    }
    out = _local_agent_fallback("继续", [], spec)
    assert out.get("is_fallback") is True
    assert out.get("local_fallback") is True
    say = out["say"]
    # no fabricated draft, no leaked internal code word
    assert "[第" not in say
    assert "本地草稿" not in say
    assert "knowledge" not in say
    assert not looks_like_ai_draft(say)


def test_fallback_turn_never_sets_candidate_script(db):
    from backend.models.studio_session import StudioSession
    from backend.services.studio_agent import StudioAgent

    sess = StudioSession()
    db.add(sess); db.commit(); db.refresh(sess)

    with patch("backend.lib.llm_client.LLMClient._call",
               side_effect=RuntimeError("upstream provider error")):
        result = StudioAgent().step(db, sess, "做一条装修技巧的 YouTube Shorts，大概1分钟")

    assert result["blocked"] is False
    assert "[第" not in result["assistant_text"]
    assert "knowledge" not in result["assistant_text"]
    db.refresh(sess)
    assert not sess.spec.get("candidate_script")


# ----------------------------------------------------------------------
# F — structured tone / angle / exclude extraction
# ----------------------------------------------------------------------

def test_intent_extract_enabled_default_and_off(monkeypatch):
    from backend.services.studio_agent import _studio_intent_extract_enabled
    monkeypatch.delenv("MB_STUDIO_INTENT_EXTRACT", raising=False)
    assert _studio_intent_extract_enabled() is True
    monkeypatch.setenv("MB_STUDIO_INTENT_EXTRACT", "0")
    assert _studio_intent_extract_enabled() is False


def test_extract_intent_tone_angle_exclude():
    from backend.services.studio_agent import _studio_extract_intent
    out = _studio_extract_intent(
        [], "做条马斯克的视频，要幽默一点，重点讲火星执念，不要讲特斯拉", {},
    )
    assert out["tone"] == "幽默诙谐"
    assert "火星" in out["angle"]
    assert "特斯拉" in out["exclude"]


def test_extract_intent_prefers_spec():
    from backend.services.studio_agent import _studio_extract_intent
    out = _studio_extract_intent(
        [], "随便写写", {"tone": "严肃", "angle": "殖民火星", "exclude": "电动车"},
    )
    assert out == {"tone": "严肃", "angle": "殖民火星", "exclude": "电动车"}


def test_extract_intent_scans_recent_turns():
    from backend.services.studio_agent import _studio_extract_intent
    msgs = [
        {"role": "user", "content": "重点讲它的伪装能力"},
        {"role": "assistant", "content": "[第 1 稿]..."},
    ]
    out = _studio_extract_intent(msgs, "再来一版", {})
    assert "伪装" in out["angle"]


def _capture_gen(store):
    def _fake_gen(prompt, output_format="youtube_landscape",
                  duration_hint_seconds=60, system_context="", speed=1.0,
                  research_pack="", **kwargs):
        # **kwargs:generate_script 后来加了 tts_provider(按音色算字数预算)。
        # 替身用 **kwargs 收尾,以后再加参数不会又把这几个测试打挂。
        store["prompt"] = prompt
        store["system_context"] = system_context
        store["research_pack"] = research_pack
        store.update(kwargs)
        return "章鱼有三个心脏。它们的血液是蓝色的。这真的太神奇了。"
    return _fake_gen


def test_first_draft_injects_tone_angle_exclude(db, monkeypatch):
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "1")
    monkeypatch.setenv("MB_STUDIO_INTENT_EXTRACT", "1")
    from backend.services.studio_agent import StudioAgent

    sess = _short_session(db)
    cap = {}
    with patch("backend.lib.llm_client.LLMClient._call", return_value=_DRAFT_JSON), \
         patch("backend.lib.llm_client.LLMClient.generate_script",
               side_effect=_capture_gen(cap)):
        StudioAgent().step(
            db, sess,
            "做一条关于章鱼的短视频，要幽默一点，重点讲它的伪装能力，不要讲价格",
        )
    sc = cap.get("system_context", "")
    assert "本条具体要求" in sc
    assert "幽默" in sc
    assert "伪装" in sc          # user angle
    assert "价格" in sc          # exclude


def test_first_draft_intent_extract_off(db, monkeypatch):
    monkeypatch.setenv("MB_STUDIO_GENERATE_SCRIPT", "1")
    monkeypatch.setenv("MB_STUDIO_INTENT_EXTRACT", "0")
    from backend.services.studio_agent import StudioAgent

    sess = _short_session(db)
    cap = {}
    with patch("backend.lib.llm_client.LLMClient._call", return_value=_DRAFT_JSON), \
         patch("backend.lib.llm_client.LLMClient.generate_script",
               side_effect=_capture_gen(cap)):
        StudioAgent().step(
            db, sess, "做一条关于章鱼的短视频，要幽默一点，重点讲它的伪装能力",
        )
    assert "本条具体要求" not in cap.get("system_context", "")


# ----------------------------------------------------------------------
# D — session-cached research pack
# ----------------------------------------------------------------------

class _FakeLLM:
    def __init__(self):
        self.build_calls = 0

    def _short_should_ground(self, topic, ctx=""):
        return True

    def _short_grounding_max_tokens(self):
        return 3000

    def _short_grounding_timeout(self):
        return 90

    def build_research_pack(self, topic, **k):
        self.build_calls += 1
        return f"PACK for {topic}: 关键事实若干。"


def test_research_pack_caches_and_reuses(monkeypatch):
    monkeypatch.setenv("MB_STUDIO_RESEARCH_CACHE", "1")
    from backend.services.studio_agent import _studio_get_research_pack
    llm = _FakeLLM()
    spec = {}
    p1, t1 = _studio_get_research_pack(llm, "章鱼", "", "ctx", spec)
    assert t1 is True and "PACK for 章鱼" in p1 and llm.build_calls == 1
    # same topic + angle → reuse cached, no new网络
    p2, t2 = _studio_get_research_pack(llm, "章鱼", "", "ctx", spec)
    assert t2 is True and p2 == p1 and llm.build_calls == 1


def test_research_pack_rebuilds_on_topic_change(monkeypatch):
    monkeypatch.setenv("MB_STUDIO_RESEARCH_CACHE", "1")
    from backend.services.studio_agent import _studio_get_research_pack
    llm = _FakeLLM()
    spec = {}
    _studio_get_research_pack(llm, "章鱼", "", "ctx", spec)
    assert llm.build_calls == 1
    _studio_get_research_pack(llm, "水母", "", "ctx", spec)   # different topic
    assert llm.build_calls == 2


def test_research_pack_disabled_does_not_take_over(monkeypatch):
    monkeypatch.setenv("MB_STUDIO_RESEARCH_CACHE", "0")
    from backend.services.studio_agent import _studio_get_research_pack
    llm = _FakeLLM()
    pack, took = _studio_get_research_pack(llm, "章鱼", "", "ctx", {})
    assert (pack, took) == ("", False)
    assert llm.build_calls == 0


def test_research_pack_skips_non_fact_topic(monkeypatch):
    monkeypatch.setenv("MB_STUDIO_RESEARCH_CACHE", "1")
    from backend.services.studio_agent import _studio_get_research_pack

    class _NoGround(_FakeLLM):
        def _short_should_ground(self, topic, ctx=""):
            return False

    llm = _NoGround()
    pack, took = _studio_get_research_pack(llm, "闲聊", "", "ctx", {})
    assert (pack, took) == ("", False)
    assert llm.build_calls == 0


# ----------------------------------------------------------------------
# G — angle routing (user angle wins; env default off)
# ----------------------------------------------------------------------









