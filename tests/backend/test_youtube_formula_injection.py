"""Long-video formula-scaffold injection (studio_agent).

Verifies that the Script Intelligence archetype's 5-layer template renders into
the system-prompt scaffold, that the anti-fabrication guardrail is always
present, and that _build_system_prompt only appends the block when supplied
(short-video behavior unchanged).
"""
import pytest

from backend.services.studio_agent import (
    _build_system_prompt,
    _classify_long_video_intent,
    _extract_json_array,
    _generate_long_script_via_outline,
    _long_video_topic_from_history,
    _looks_like_generate_keyword,
    _looks_like_long_video_write_confirm,
    build_fact_firewall,
    _render_formula_block,
)


class _FakeLLM:
    """Records calls; returns scripted outline + section bodies.

    First call (outline) → JSON array; later calls (sections) → ~long paragraph.
    """

    def __init__(self, n_sections=8):
        self.calls = []
        self._n = n_sections

    def _call(self, system, user, *, purpose=None):
        self.calls.append({"purpose": purpose})
        if len(self.calls) == 1:
            import json
            return json.dumps(
                [{"beat": "纠正常识", "focus": f"第{i}节"} for i in range(self._n)],
                ensure_ascii=False,
            )
        return "黑曼巴其实并不是因为体色得名。" * 18


def _sample_tpl() -> dict:
    return {
        "narrative_arc": ["熟悉现象切入", "纠正常识误区", "回扣主题"],
        "beat_skeletons": [
            {"beat": "熟悉现象切入", "skeleton": "很多人都会遇到 ___"},
        ],
        "signature_phrases": [
            "其实并不是 ___",
            "更离谱的是 ___",
            "最可怕的不是 ___，而是 ___",
        ],
        "full_skeleton": "你有没有见过 ___？",
        "topic_criteria": ["大家见过但不真正了解"],
        "fact_slots": ["寿命/时长数字"],
    }


def test_render_formula_block_contains_all_layers_and_fact_rule():
    block = _render_formula_block("猎奇反转原型", _sample_tpl())

    assert "猎奇反转原型" in block
    assert "叙事顺序" in block
    assert "1. 熟悉现象切入" in block
    assert "2. 纠正常识误区" in block
    assert "3. 回扣主题" in block
    assert "强反差句式" in block
    # signature phrase blanks present (at least 3 fill-ins)
    assert block.count("___") >= 3
    assert "选题标准" in block
    # anti-fabrication guardrail
    assert "事实槽" in block
    assert "绝对禁止" in block


def test_render_formula_block_guardrail_present_without_fact_slots():
    tpl = _sample_tpl()
    tpl.pop("fact_slots")

    block = _render_formula_block("x", tpl)

    # Guardrail must persist even when the archetype omits fact_slots.
    assert "绝对禁止" in block


def test_render_formula_block_handles_empty_template():
    block = _render_formula_block("空原型", {})

    # No layers, but the guardrail and header still render (no crash).
    assert "长视频写作骨架" in block
    assert "绝对禁止" in block


def test_fact_firewall_blocks_titles_subheads_and_fake_attachments():
    block = build_fact_firewall("黑洞内部结构", "youtube_long")

    assert "全网最全" in block
    assert "N秒讲清" in block
    assert "实战演示" in block
    assert "常见坑" in block
    assert "我整理好了文档/脚本/表格/资料包/清单/计算器" in block


def test_build_system_prompt_appends_formula_block_only_when_supplied():
    spec = {"platform": "youtube_long", "duration_seconds": 600}
    marker = "\n\n__MARKER_FORMULA_BLOCK__"

    with_block = _build_system_prompt(spec, formula_block=marker)
    without_block = _build_system_prompt(spec)

    assert "__MARKER_FORMULA_BLOCK__" in with_block
    assert "__MARKER_FORMULA_BLOCK__" not in without_block
    # base prompt identical whether block is "" or omitted (short-video path)
    assert without_block == _build_system_prompt(spec, formula_block="")


def test_extract_json_array_tolerates_fences():
    assert _extract_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert _extract_json_array('随便说点 ["x","y"] 结尾') == ["x", "y"]


class _IntentLLM:
    """Fake LLM returning a scripted intent-classification JSON."""

    def __init__(self, intent, topic=""):
        import json as _json
        self._resp = _json.dumps({"intent": intent, "topic": topic})

    def _call(self, system, user, *, purpose=None):
        return self._resp


def test_classify_intent_generate_returns_topic():
    intent, topic = _classify_long_video_intent(
        _IntentLLM("GENERATE", "黑曼巴蛇"), "做个黑曼巴的视频"
    )
    assert intent == "GENERATE"
    assert topic == "黑曼巴蛇"


def test_classify_intent_chat_returns_empty():
    intent, topic = _classify_long_video_intent(
        _IntentLLM("CHAT", ""), "我们能聊一下剧本方面的内容吗"
    )
    assert intent == "CHAT"
    assert topic == ""


def test_classify_intent_clarify_returns_no_topic():
    intent, topic = _classify_long_video_intent(
        _IntentLLM("CLARIFY", ""), "黑曼巴"
    )
    assert intent == "CLARIFY"
    assert topic == ""


def test_classify_intent_falls_back_to_keywords_on_llm_failure():
    class _BrokenLLM:
        def _call(self, *a, **k):
            raise RuntimeError("upstream down")

    # produce keyword → GENERATE via fallback
    intent, topic = _classify_long_video_intent(_BrokenLLM(), "做个黑曼巴的视频")
    assert intent == "GENERATE" and topic
    # meta question → CHAT via fallback
    intent2, _ = _classify_long_video_intent(_BrokenLLM(), "我们能聊一下剧本吗")
    assert intent2 == "CHAT"


def test_keyword_fallback_classifies_task_vs_chat():
    assert _looks_like_generate_keyword("做个黑曼巴的视频") is True
    assert _looks_like_generate_keyword("帮我写一篇关于狼群的稿子") is True
    assert _looks_like_generate_keyword("我们能聊一下剧本方面的内容吗") is False
    assert _looks_like_generate_keyword("你好") is False


def test_long_video_write_confirmation_is_not_a_new_topic():
    assert _looks_like_long_video_write_confirm("写吧") is True
    assert _looks_like_long_video_write_confirm("直接写") is True
    assert _looks_like_long_video_write_confirm("你好") is False


def test_long_video_write_confirmation_uses_previous_topic():
    messages = [
        {"role": "user", "content": "你好，我想做一个关于动物类的视频，有什么推荐的"},
        {"role": "user", "content": "有什么动物类的，猪可以吗？最好是野猪"},
        {"role": "user", "content": "知识科普类的"},
        {"role": "user", "content": "写吧"},
    ]

    topic = _long_video_topic_from_history(messages, "写吧", {})

    assert topic == "野猪"


def test_generate_long_script_outline_then_expand_reaches_length():
    llm = _FakeLLM(n_sections=8)

    script = _generate_long_script_via_outline(
        llm, "黑曼巴蛇", "（骨架占位）", target_chars=2600, duration_seconds=600,
    )

    # 1 outline call + one call per outlined section
    assert len(llm.calls) >= 9
    # every call routes to the long-video model purpose (Qwen) — isolation
    assert all(c["purpose"] == "studio_long" for c in llm.calls)
    assert len(script) >= int(2600 * 0.6)
    assert "[第" not in script  # no draft markers leaked from sections


def test_generate_long_script_raises_when_sections_too_short():
    class _ShortLLM(_FakeLLM):
        def _call(self, system, user, *, purpose=None):
            self.calls.append({"purpose": purpose})
            if len(self.calls) == 1:
                import json
                return json.dumps([{"beat": "x", "focus": "y"}])
            return "太短了"

    with pytest.raises(ValueError):
        _generate_long_script_via_outline(_ShortLLM(), "主题", "", target_chars=2600)
