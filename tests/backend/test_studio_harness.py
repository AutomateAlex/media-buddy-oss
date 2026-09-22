from unittest.mock import patch

import pytest


FRACTAL_SCRIPT = (
    "一根线能把一维的宇宙折出来！最初，1890 年数学家皮亚诺用简单的分割方式，"
    "画出了一根线竟然能填满一个正方形。这根线反映出一个惊人的事实，维度并不是我们想象的那么简单。"
    "而科赫曲线的维度竟然是 1.26 维。分形结构的局部包含了整体的信息，比如肺里的支气管和树木的形状，"
    "都是在有限空间内实现最大化覆盖的最佳方案。分形可能就是宇宙书写自己源代码的语言。"
    "你的肺泡总面积大约等于半个网球场，层层分叉的血管总长度超过十万公里。评论告诉我，你最认可哪个分形的例子？"
)


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
    from backend.models.project import Project
    from backend.models.studio_session import StudioSession
    from backend.lib.agent_safety import _msg_log

    db.query(Project).delete()
    db.query(StudioSession).delete()
    db.commit()
    _msg_log.clear()
    yield


def _session(db, *, spec=None, messages=None):
    from backend.models.studio_session import StudioSession

    sess = StudioSession(spec=spec or {}, messages=messages or [])
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def _configured_spec():
    return {
        "video_type": "tutorial",
        "direction": "science_explainer",
        "platform": "youtube_shorts",
        "aspect_ratio": "9:16",
        "output_format": "youtube_shorts",
        "duration_seconds": 60,
        "count": 1,
        "voice": "qwen_cherry",
        "voice_label": "Yichen Wang · 清亮中文旁白",
        "voice_speed": 1.0,
    }


def test_machine_context_not_treated_as_script():
    from backend.services.studio_flow import looks_like_script, update_candidate_from_user, classify_studio_message

    spec = {}
    msg = '【MACHINE_CONTEXT_JSON】{"module":"short_video","aspect_ratio":"9:16"}【/MACHINE_CONTEXT_JSON】'
    decision = classify_studio_message(msg, spec)
    update_candidate_from_user(spec, msg, decision)

    assert looks_like_script(msg) is False
    assert spec.get("candidate_script") is None


def test_paste_then_submit_produces_vertical(db):
    from backend.models.project import Project
    from backend.services.studio_agent import StudioAgent

    sess = _session(db)
    agent = StudioAgent()
    agent.step(db, sess, FRACTAL_SCRIPT)
    agent.step(db, sess, "YouTube Shorts")

    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work"):
        result = agent.step(db, sess, "提交")

    assert result["tool_call"]["name"] == "submit_project"
    project = db.query(Project).filter(Project.id == result["tool_call"]["result"]["project_id"]).first()
    assert project is not None
    assert project.output_format == "youtube_shorts"
    assert sess.spec["aspect_ratio"] == "9:16"
    assert sess.spec["candidate_script"].startswith("一根线能把一维")


def test_paste_then_start_produces(db):
    from backend.services.studio_agent import StudioAgent

    sess = _session(db)
    agent = StudioAgent()
    agent.step(db, sess, FRACTAL_SCRIPT)
    agent.step(db, sess, "YouTube Shorts")

    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work"):
        result = agent.step(db, sess, "开始生成")

    assert result["tool_call"]["name"] == "submit_project"


def test_no_script_then_start_asks_for_script(db):
    from backend.services.studio_agent import StudioAgent

    sess = _session(db, spec={"platform": "youtube_shorts"})
    result = StudioAgent().step(db, sess, "开始生成")

    assert result["tool_call"] is None
    assert "稿" in result["assistant_text"] or "脚本" in result["assistant_text"]


def test_iterate_replaces_candidate(db):
    from backend.services.studio_agent import StudioAgent

    sess = _session(db, spec=_configured_spec(), messages=[{"role": "user", "content": FRACTAL_SCRIPT}])
    first = FRACTAL_SCRIPT
    reply = (
        "[第 2 稿 · 约 230 字 ≈ 55 秒]\n\n"
        "一根线竟然能填满一个正方形。分形真正吓人的地方，是它让维度不再像尺子一样简单。"
        "皮亚诺曲线、科赫曲线、树枝和肺部支气管，都在重复同一种秘密：局部藏着整体。"
        "有限空间里，自然会用分叉和自相似，把覆盖效率推到极限。你的肺泡面积接近半个网球场，"
        "血管长度能绕地球两圈半。分形不是图案，而像宇宙写给自己的压缩算法。"
        "评论告诉我，你最震撼的分形例子是什么？"
    )
    with patch("backend.lib.llm_client.LLMClient._call", return_value='{"say": ' + __import__("json").dumps(reply, ensure_ascii=False) + ', "spec_updates": {}, "tool_call": null}'):
        result = StudioAgent().step(db, sess, "改短一点")

    assert result["tool_call"] is None
    assert sess.spec["candidate_script"] != first
    assert sess.spec["candidate_script"].startswith("一根线竟然")
    assert len(sess.spec["script_history"]) >= 2


def test_use_first_draft_switches_candidate():
    from backend.services.studio_flow import classify_studio_message, update_candidate_from_user

    spec = {
        "platform": "youtube_shorts",
        "script_history": [
            {"text": "第一稿内容很长。" * 12, "source": "ai", "version": 1},
            {"text": "第二稿内容很长。" * 12, "source": "ai", "version": 2},
        ],
        "candidate_script": "第二稿内容很长。" * 12,
    }
    decision = classify_studio_message("用第一稿", spec)
    update_candidate_from_user(spec, "用第一稿", decision)

    assert decision.intent == "PRODUCE"
    assert spec["candidate_script"].startswith("第一稿")


def test_first_draft_confirm_produces_without_lock_step(db):
    from backend.models.project import Project
    from backend.services.studio_agent import StudioAgent

    draft = "[第 1 稿 · 约 260 字 ≈ 60 秒]\n\n" + FRACTAL_SCRIPT
    spec = _configured_spec()
    spec["script_history"] = [{"text": FRACTAL_SCRIPT, "source": "ai", "version": 1}]
    sess = _session(db, spec=spec, messages=[{"role": "assistant", "content": draft}])

    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work"):
        result = StudioAgent().step(db, sess, "第一稿 这个不错")

    assert result["tool_call"]["name"] == "submit_project"
    project = db.query(Project).filter(Project.id == result["tool_call"]["result"]["project_id"]).first()
    assert project is not None
    assert project.script_text.startswith("一根线能把一维")
    assert project.output_format == "youtube_shorts"




def test_ai_draft_updates_candidate():
    from backend.services.studio_flow import update_candidate_from_ai

    spec = {}
    update_candidate_from_ai(spec, "[第 1 稿 · 约 260 字 ≈ 60 秒]\n\n" + FRACTAL_SCRIPT)

    assert spec["candidate_script"].startswith("一根线能把一维")
    assert spec["last_assistant_action"] == "showed_draft"


def test_square_context_is_removed_from_short_video(db):
    from backend.models.project import Project
    from backend.services.studio_agent import StudioAgent

    msg = (
        '【MACHINE_CONTEXT_JSON】{"module":"short_video","voice":"qwen_cherry",'
        '"voice_speed":1,"aspect_ratio":"1:1","output_format":"instagram_feed",'
        '"video_format":"方形 1:1","platforms":["YouTube Shorts"],"duration_label":"1 分钟","count":1}'
        '【/MACHINE_CONTEXT_JSON】\n\n【用户最新输入】\n开始生成'
    )
    spec = _configured_spec()
    spec["candidate_script"] = FRACTAL_SCRIPT
    spec["candidate_preview"] = FRACTAL_SCRIPT[:60]
    sess = _session(db, spec=spec)

    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work"):
        result = StudioAgent().step(db, sess, msg)

    project = db.query(Project).filter(Project.id == result["tool_call"]["result"]["project_id"]).first()
    assert project.output_format == "youtube_shorts"
    assert sess.spec["aspect_ratio"] == "9:16"


def test_paste_then_platform_auto_generates_draft(db):
    from backend.services.studio_agent import StudioAgent

    sess = _session(db)
    agent = StudioAgent()
    agent.step(db, sess, FRACTAL_SCRIPT)
    draft = (
        "[第 1 稿 · 约 260 字 ≈ 60 秒]\n\n"
        "一根线能把一维宇宙折进一个正方形。1890 年，皮亚诺用不断分割的方式，"
        "画出一条几乎填满平面的曲线，让维度这件事突然变得不再直观。更夸张的是科赫曲线，"
        "它的维度不是 1，也不是 2，而是 1.26。分形最迷人的地方，在于局部藏着整体。"
        "你看树枝、河流、肺里的支气管，都是一层层分叉，在有限空间里覆盖更多面积。"
        "你的肺泡总面积接近半个网球场，血管长度能绕地球两圈半。分形不是漂亮图案，"
        "它更像自然写给自己的效率公式。评论告诉我，你还在哪里见过分形？"
    )

    with patch("backend.lib.llm_client.LLMClient._call", return_value='{"say": ' + __import__("json").dumps(draft, ensure_ascii=False) + ', "spec_updates": {}, "tool_call": null}'):
        result = agent.step(db, sess, "YouTube Shorts")

    assert "[第 1 稿" in result["assistant_text"]
    assert any(v.get("source") == "ai" for v in sess.spec["script_history"])
    assert sess.spec["last_assistant_action"] == "showed_draft"
    assert sess.spec["candidate_script"].startswith("一根线能把一维宇宙")


def test_status_question_does_not_ask_clarification(db):
    from backend.services.studio_agent import StudioAgent

    spec = _configured_spec()
    spec["candidate_script"] = FRACTAL_SCRIPT
    spec["candidate_preview"] = FRACTAL_SCRIPT[:60]
    spec["last_assistant_action"] = "showed_draft"
    sess = _session(db, spec=spec, messages=[{"role": "assistant", "content": "[第 1 稿 · 约 260 字 ≈ 60 秒]\n\n" + FRACTAL_SCRIPT}])

    result = StudioAgent().step(db, sess, "好了吗？")

    assert result["tool_call"] is None
    assert "你是指" not in result["assistant_text"]
    assert "请直接说" not in result["assistant_text"]
    assert "脚本开头" in result["assistant_text"] or "当前稿" in result["assistant_text"]


def test_explicit_horizontal_format_is_respected(db):
    from backend.models.project import Project
    from backend.services.studio_agent import StudioAgent

    msg = (
        '【MACHINE_CONTEXT_JSON】{"module":"short_video","voice":"qwen_cherry",'
        '"voice_speed":1,"aspect_ratio":"16:9","output_format":"youtube_landscape",'
        '"video_format":"横版 16:9","platforms":["YouTube"],"duration_label":"1 分钟","count":1}'
        '【/MACHINE_CONTEXT_JSON】\n\n【用户最新输入】\n开始生成'
    )
    spec = _configured_spec()
    spec["candidate_script"] = FRACTAL_SCRIPT
    spec["candidate_preview"] = FRACTAL_SCRIPT[:60]
    sess = _session(db, spec=spec)

    with patch("backend.workers.background_worker.BackgroundWorker.notify_new_work"):
        result = StudioAgent().step(db, sess, msg)

    project = db.query(Project).filter(Project.id == result["tool_call"]["result"]["project_id"]).first()
    assert project.output_format == "youtube_landscape"
    assert sess.spec["aspect_ratio"] == "16:9"
