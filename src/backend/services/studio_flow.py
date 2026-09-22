"""Small state machine for the short-video Studio Agent.

The production entrance is intentionally simple:
- the latest usable script is `candidate_script`
- user intent is one of PRODUCE / ITERATE / CHAT
- short-video platforms always produce vertical 9:16 output
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Any, Literal, Optional


Intent = Literal["PRODUCE", "ITERATE", "CHAT"]


@dataclass
class StudioIntentDecision:
    intent: Intent
    config_updates: dict[str, Any]
    draft_switch: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PLATFORM_DEFAULTS = {
    "youtube_shorts": {"aspect_ratio": "9:16", "duration_seconds": 60, "output_format": "youtube_shorts"},
    "tiktok": {"aspect_ratio": "9:16", "duration_seconds": 60, "output_format": "tiktok"},
    "reels": {"aspect_ratio": "9:16", "duration_seconds": 60, "output_format": "instagram_reels"},
    "youtube_long": {"aspect_ratio": "16:9", "duration_seconds": 600, "output_format": "youtube_landscape"},
}

SHORT_PLATFORMS = {"youtube_shorts", "tiktok", "reels"}


def visible_user_text(content: str) -> str:
    text = re.sub(r"【MACHINE_CONTEXT_JSON】[\s\S]*?【/MACHINE_CONTEXT_JSON】\s*", "", str(content or ""))
    if "【用户最新输入】" in text:
        text = text.split("【用户最新输入】")[-1]
    return text.strip()


def apply_machine_context(spec: dict[str, Any], machine_context: dict[str, Any]) -> None:
    if not machine_context:
        return
    if machine_context.get("voice"):
        spec["voice"] = str(machine_context["voice"])
    if machine_context.get("voice_label"):
        spec["voice_label"] = str(machine_context["voice_label"])
    if machine_context.get("voice_speed"):
        spec["voice_speed"] = float(machine_context["voice_speed"])
    # 字幕语言 + 字幕字号缩放系数(客户拖拉杆自选)——studio 聊天流历史上漏传这两个,
    # 一并补上,否则「长视频编导助手」出片拿不到用户选的字幕设置。
    if machine_context.get("subtitle_language"):
        spec["subtitle_language"] = str(machine_context["subtitle_language"])
    #    用 `.get(...)` 的真假判断会把「没传」当成关,老用户的视频会突然全没字幕。
    if machine_context.get("include_subtitles") is False:
        spec["include_subtitles"] = False
    if machine_context.get("subtitle_font_scale"):
        try:
            spec["subtitle_font_scale"] = max(0.5, min(2.0, float(machine_context["subtitle_font_scale"])))
        except (TypeError, ValueError):
            pass
    if machine_context.get("count"):
        try:
            spec["count"] = max(1, int(machine_context["count"]))
        except Exception:
            pass
    explicit_duration = machine_context.get("duration_seconds") or machine_context.get("target_duration_seconds")
    duration = None
    if explicit_duration:
        try:
            duration = max(1, int(explicit_duration))
        except Exception:
            duration = None
    duration = duration or duration_seconds_from_label(str(machine_context.get("duration_label") or ""))
    if duration:
        # Product cap: a single video maxes out at 30 min (1800s). Longer
        # synchronous chat generation can't beat the proxy timeout. (Short
        # videos are always < 1800s, so this clamp never touches them.)
        spec["duration_seconds"] = min(1800, duration)
    platforms = [str(p).lower() for p in (machine_context.get("platforms") or [])]
    explicit_output = str(machine_context.get("output_format") or "").lower()
    explicit_aspect = str(machine_context.get("aspect_ratio") or "").strip()
    if explicit_output == "youtube_landscape" or explicit_aspect == "16:9":
        spec["platform"] = "youtube_long"
    elif explicit_output in {"youtube_shorts", "tiktok", "instagram_reels"} or explicit_aspect == "9:16":
        if any("tiktok" in p or "抖音" in p for p in platforms):
            spec["platform"] = "tiktok"
        elif any("reels" in p or "instagram" in p for p in platforms):
            spec["platform"] = "reels"
        else:
            spec["platform"] = "youtube_shorts"
    elif not spec.get("platform"):
        if explicit_output == "youtube_landscape" or explicit_aspect == "16:9":
            spec["platform"] = "youtube_long"
        elif any("youtube" in p and "short" in p for p in platforms):
            spec["platform"] = "youtube_shorts"
        elif any("tiktok" in p or "抖音" in p for p in platforms):
            spec["platform"] = "tiktok"
        elif any("reels" in p or "instagram" in p for p in platforms):
            spec["platform"] = "reels"
    apply_platform_defaults(spec)


def duration_seconds_from_label(label: str) -> Optional[int]:
    text = str(label or "")
    # 短视频「秒」档:含"秒"且不含"分钟" → 按秒解析(取数字上限)。必须在分钟范围解析之前,
    # 否则 "45-60 秒" / "30-45 秒" 会掉进下面被当成 45-60 / 30-45 **分钟** → 2700/3600s
    # → 被截到 1800s → 误判成长视频、系统提示词切成"30分钟长视频稿"(生产 bug)。
    if "秒" in text and "分钟" not in text:
        import re
        _nums = [int(n) for n in re.findall(r"\d+", text)]
        return max(_nums) if _nums else None
    if "45-60" in text or ("45" in text and "60" in text):
        return 3600
    if "30-45" in text or ("30" in text and "45" in text):
        return 2700
    if "20-30" in text or ("20" in text and "30" in text):
        return 1800
    if "12-20" in text or ("12" in text and "20" in text):
        return 1200
    if "8-12" in text or ("8" in text and "12" in text) or "10" in text or "十" in text or "约" in text or "左右" in text:
        return 600
    if "5-8" in text or ("5" in text and "8" in text):
        return 480
    if "3-5" in text or "3 到 5" in text:
        return 300
    if "3" in text and "分钟" in text:
        return 180
    if "2" in text and "分钟" in text:
        return 120
    if "1" in text and "分钟" in text:
        return 60
    if "45-60" in text:
        return 60
    if "30-45" in text:
        return 45
    if "15-30" in text:
        return 30
    if "15" in text:
        return 15
    return None


def apply_platform_defaults(spec: dict[str, Any]) -> None:
    platform = str(spec.get("platform") or "").strip()
    if platform not in PLATFORM_DEFAULTS:
        return
    defaults = PLATFORM_DEFAULTS[platform]
    spec["aspect_ratio"] = defaults["aspect_ratio"]
    spec["output_format"] = defaults["output_format"]
    if not spec.get("duration_seconds"):
        spec["duration_seconds"] = defaults["duration_seconds"]
    if not spec.get("count"):
        spec["count"] = 1
    if not spec.get("voice"):
        spec["voice"] = "azure_yunyang"
    if not spec.get("voice_speed"):
        spec["voice_speed"] = 1.0


def output_format_for_spec(spec: dict[str, Any]) -> str:
    apply_platform_defaults(spec)
    return str(spec.get("output_format") or "youtube_shorts")


def config_complete(spec: dict[str, Any]) -> bool:
    return bool(spec.get("platform"))


def has_ai_draft(spec: dict[str, Any]) -> bool:
    return any((item or {}).get("source") == "ai" for item in list(spec.get("script_history") or []))


def should_auto_generate_draft(spec: dict[str, Any], decision: StudioIntentDecision, had_platform: bool) -> bool:
    return (
        not had_platform
        and config_complete(spec)
        and bool(spec.get("candidate_script"))
        and spec.get("candidate_source") == "user"
        and not has_ai_draft(spec)
        and decision.intent != "PRODUCE"
    )


def is_status_question(user_message: str) -> bool:
    text = re.sub(r"[\s。！？!?.，,、]+", "", visible_user_text(user_message)).lower()
    return text in {"好了吗", "好了没", "写好了吗", "脚本好了吗", "稿子好了吗", "done", "ready", "isitready"}


def format_current_draft_status(spec: dict[str, Any]) -> str:
    if spec.get("candidate_script"):
        return (
            "好了，当前稿已经在这里。\n\n"
            f"脚本开头：{script_preview(str(spec.get('candidate_script') or ''), 90)}\n\n"
            "要继续改就直接说改哪里；觉得可以，就说“开始生成”。"
        )
    pending = dict(spec.get("pending_long_draft") or {})
    stage = str(pending.get("stage") or "").strip().lower()
    topic = str(pending.get("topic") or "这条长稿").strip()
    if stage == "queued":
        age_seconds = 0.0
        try:
            started = datetime.fromisoformat(str(pending.get("started_at") or ""))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except (TypeError, ValueError):
            pass
        if age_seconds > 180:
            return (
                f"“{topic}”的后台任务排队已超过 3 分钟，仍未开始联网查证。"
                "写稿 worker 可能离线；系统没有在后台继续生成，请稍后重试或联系管理员。"
            )
        return f"“{topic}”已经排队，正在等待后台写稿服务领取，尚未开始联网查证。"
    if stage == "researching":
        return f"“{topic}”正在联网查证资料和案例，还没有进入正式写稿。"
    if stage == "drafting":
        return f"“{topic}”已经完成资料查证，正在撰写完整长稿。"
    if stage == "quality_checking":
        return f"“{topic}”的完整稿已经写出，正在检查长度和结尾完整性。"
    if stage == "failed":
        reason = str(pending.get("error") or "后台写稿失败").strip()
        return f"“{topic}”没有生成成功：{reason}。请直接说“重试”再来一次。"
    if stage == "completed":
        return (
            f"“{topic}”的任务标记为完成，但稿件没有正确写回当前会话。"
            "请联系管理员检查任务写回。"
        )
    return "还没拿到完整稿子。你可以先贴一段脚本，或者告诉我主题，我来写一版。"


def classify_studio_message(user_message: str, spec: dict[str, Any]) -> StudioIntentDecision:
    text = visible_user_text(user_message)
    lower = text.lower()
    # 贴的是整段脚本时,正文里的"TikTok/YouTube/一分钟/横版"等是【内容】而非配置指令——
    # 不能拿来改平台/格式(否则贴含"TikTok"的长视频稿会被翻成竖屏短视频)。
    config_updates = {} if looks_like_script(text) else _extract_config_updates(text)
    draft_switch = _extract_draft_switch(text)

    # 贴稿确认态:客户贴了自己的完整文案后回"不用改 / 直接生成 / 就这样出" → 直接产片。
    # 必须放在 _is_iterate 之前——否则"不用改"里的"改"会被当成改稿指令,产片被吞。
    if _is_user_script_produce(text, spec):
        return StudioIntentDecision("PRODUCE", config_updates, draft_switch)

    if _is_iterate(text, lower):
        return StudioIntentDecision("ITERATE", config_updates, draft_switch)

    if script_acceptance_only(text):
        return StudioIntentDecision("CHAT", config_updates, draft_switch)

    if _is_produce(text, lower, spec, draft_switch):
        return StudioIntentDecision("PRODUCE", config_updates, draft_switch)

    return StudioIntentDecision("CHAT", config_updates, draft_switch)


def apply_config_updates(spec: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in (updates or {}).items():
        if value not in (None, ""):
            spec[key] = value
    apply_platform_defaults(spec)


def update_candidate_from_user(spec: dict[str, Any], user_message: str, decision: StudioIntentDecision) -> None:
    if decision.draft_switch is not None:
        record = history_record(spec, decision.draft_switch)
        if record:
            spec["candidate_script"] = record["text"]
            spec["candidate_source"] = record.get("source") or "ai"
            spec["candidate_preview"] = script_preview(record["text"])
            return

    text = extract_user_script_text(visible_user_text(user_message))
    if looks_like_script(text):
        set_candidate_script(spec, text, "user")


def hydrate_candidate_from_history(spec: dict[str, Any], messages: list[dict]) -> None:
    if spec.get("candidate_script"):
        return
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        text = str(msg.get("content") or "")
        if looks_like_ai_draft(text):
            body = extract_script_body(text)
            if looks_like_script(body):
                set_candidate_script(spec, body, "ai")
                return
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        text = extract_user_script_text(visible_user_text(str(msg.get("content") or "")))
        if looks_like_script(text):
            set_candidate_script(spec, text, "user")
            return


def use_latest_user_script_from_history(spec: dict[str, Any], messages: list[dict]) -> bool:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        text = extract_user_script_text(visible_user_text(str(msg.get("content") or "")))
        if looks_like_script(text):
            set_candidate_script(spec, text, "user")
            return True
    return False


def update_candidate_from_ai(spec: dict[str, Any], ai_response: str) -> None:
    if not looks_like_ai_draft(ai_response):
        spec["last_assistant_action"] = "chatting"
        return
    text = extract_script_body(ai_response)
    if looks_like_script(text):
        set_candidate_script(spec, text, "ai")
        spec["last_assistant_action"] = "showed_draft"
    else:
        spec["last_assistant_action"] = "chatting"


def set_candidate_script(spec: dict[str, Any], text: str, source: Literal["user", "ai"]) -> None:
    body = extract_script_body(text).strip()
    spec["candidate_script"] = body
    spec["candidate_source"] = source
    spec["candidate_preview"] = script_preview(body)
    history = list(spec.get("script_history") or [])
    if not history or history[-1].get("text") != body:
        history.append({
            "text": body,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": len(history) + 1,
        })
    spec["script_history"] = history[-20:]


def history_record(spec: dict[str, Any], version: int) -> Optional[dict[str, Any]]:
    history = list(spec.get("script_history") or [])
    if version <= 0 or version > len(history):
        return None
    return history[version - 1]


def script_preview(text: str, max_chars: int = 64) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:max_chars] + ("..." if len(compact) > max_chars else "")


def format_production_preview(spec: dict[str, Any]) -> str:
    apply_platform_defaults(spec)
    return (
        "我现在按这个生产：\n"
        f"平台：{platform_display(spec.get('platform'))}\n"
        f"比例：{spec.get('aspect_ratio')}\n"
        f"时长：{int(spec.get('duration_seconds') or 60)} 秒\n"
        f"配音：{spec.get('voice_label') or spec.get('voice') or 'azure_yunyang'} / 语速 {spec.get('voice_speed') or 1.0}x\n\n"
        f"脚本开头：{script_preview(str(spec.get('candidate_script') or ''), 80)}\n\n"
        "开始生产。"
    )


def platform_display(platform: Any) -> str:
    return {
        "youtube_shorts": "YouTube Shorts",
        "tiktok": "TikTok / 抖音",
        "reels": "Instagram Reels",
        "youtube_long": "YouTube 长视频",
    }.get(str(platform or ""), str(platform or "待确认"))


def extract_user_script_text(text: str) -> str:
    body = str(text or "").strip()
    for marker in ("这是我的剧本", "这是我的脚本", "这是我的文案", "我的剧本", "我的脚本", "我的文案"):
        if marker in body:
            body = body.split(marker, 1)[-1].strip(" ：:\n\t")
    return body


def extract_script_body(text: str) -> str:
    body = str(text or "").strip()
    body = re.sub(r"^\s*\[\s*第\s*[一二三四五六七八九十\d]+\s*[稿版][^\]]*\]\s*", "", body)
    body = re.sub(r"^\s*这是第\s*[一二三四五六七八九十\d]+\s*[稿版][^\n]*[:：]?\s*", "", body)
    # 剥掉编导续写给用户的「改稿建议」尾巴——否则它会随 candidate_script 一路进
    # 覆盖两种尾巴:对话LLM的「要改吗?…觉得行直接说一声就提交」和 generate_script
    # 包装的「想改哪里就直接说…觉得可以就说「开始生成」」。命中即从该处整段截掉。
    _footer_markers = (
        "要改吗", "想改哪里", "继续改还是", "需要怎么改", "还想怎么改",
        "还需要再改", "需要再改", "需要我改", "要不要改",
        "觉得行直接", "觉得可以就", "满意就说", "满意就提交",
        "（这版改了", "(这版改了",
    )
    _cuts = [c for c in (body.find(_m) for _m in _footer_markers) if c != -1]
    if _cuts:
        body = body[:min(_cuts)]
    return body.strip()


def looks_like_ai_draft(text: str) -> bool:
    return bool(re.search(r"^\s*(\[\s*第\s*[一二三四五六七八九十\d]+\s*[稿版]|这是第\s*[一二三四五六七八九十\d]+\s*[稿版])", str(text or "")))


def looks_like_script(text: str) -> bool:
    body = visible_user_text(text)
    if "MACHINE_CONTEXT_JSON" in body:
        return False
    stripped = body.strip()
    if not stripped or stripped.startswith("{") or stripped.startswith("["):
        return False
    compact = re.sub(r"\s+", "", stripped)
    if len(compact) < 50:
        return False
    parts = [p.strip() for p in re.split(r"[。！？.!?]+", stripped) if p.strip()]
    if not parts:
        return False
    questions = sum(1 for p in parts if p.endswith(("?", "？")))
    # 0.6(原 0.7):脚本常带反问/设问钩子,阈值太高会把真脚本漏判成聊天。
    return (len(parts) - questions) / max(1, len(parts)) > 0.6


def _extract_config_updates(text: str) -> dict[str, Any]:
    lower = text.lower()
    updates: dict[str, Any] = {}
    if "youtube shorts" in lower or "youtube short" in lower or "youtube短视频" in lower or "shorts" in lower:
        updates["platform"] = "youtube_shorts"
    elif "tiktok" in lower or "抖音" in text:
        updates["platform"] = "tiktok"
    elif "reels" in lower or "instagram" in lower:
        updates["platform"] = "reels"
    elif "youtube" in lower or "长视频" in text:
        updates["platform"] = "youtube_long"
    if "3-5" in text or "三到五分钟" in text:
        updates["duration_seconds"] = 300
    elif "3分钟" in text or "三分钟" in text:
        updates["duration_seconds"] = 180
    elif "2分钟" in text or "两分钟" in text or "二分钟" in text:
        updates["duration_seconds"] = 120
    elif "1分钟" in text or "一分钟" in text or "60秒" in text:
        updates["duration_seconds"] = 60
    return updates


def _extract_draft_switch(text: str) -> Optional[int]:
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*[稿版]", text)
    if not m:
        return None
    return _cn_number(m.group(1))


def _cn_number(raw: str) -> Optional[int]:
    value = str(raw or "").strip()
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value in digits:
        return digits[value]
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1 if not left else 0)
        ones = digits.get(right, 0) if right else 0
        total = tens * 10 + ones
        return total or None
    return None


def _is_iterate(text: str, lower: str) -> bool:
    """Match feedback that warrants a 'reconsider the current draft' LLM turn.

    Includes BOTH specific edit requests (改/重写/...) AND vague approval
    phrases (还不错/挺好/凑合/OK). The injected ITERATE prompt in
    studio_agent.py:1721 handles the split:
      - specific change → LLM actually rewrites
      - vague approval → LLM replies "OK 吗？想改 XX 还是开始生成？"
        without faking a new [第 N 稿] (the original bug).
    Soft approvals routed through ITERATE because they need the same
    "don't echo" guard that explicit refinements get.
    """
    if any(k in text for k in ("改", "重写", "调整", "优化", "删掉", "加长", "缩短", "短一点", "长一点", "再来一版")):
        return True
    if any(k in lower for k in ("rewrite", "revise", "change", "shorter", "longer", "punchier")):
        return True
    # Vague approval / lukewarm acknowledgement — also routed through
    # ITERATE so the agent's injected prompt can reply without faking a
    # new draft. Not the same as explicit "OK go" (those are produce
    # triggers in _has_produce_trigger).
    if any(k in text for k in ("还不错", "挺好", "很好", "没问题", "可以的", "凑合", "差不多", "嗯嗯", "先这样")):
        return True
    compact = re.sub(r"\s+", "", lower)
    if compact in {"嗯", "好的", "可以", "ok", "okay"}:
        return True
    return False


def _is_produce(text: str, lower: str, spec: dict[str, Any], draft_switch: Optional[int]) -> bool:
    """Detect produce-now intent.

    used to auto-trigger PRODUCE. Those caused the agent to misfire: user
    means "this draft is OK, what next?" but agent interpreted as "submit
    right now", then the strict line-1773 gate bounced the tool call and
    user got "我先把这段当稿子记下来了…" UX dead-end.

    Now PRODUCE only fires on EXPLICIT produce triggers ("就这样" /
    "开始生成" / "执行" / "go" etc. — see _has_produce_trigger), OR
    explicit draft switch ("用第 2 稿"). Soft approvals route through
    LLM (intent=CHAT) which lets the LLM decide whether to call
    submit_project itself based on context.
    """
    if draft_switch is not None:
        return True
    if _has_produce_trigger(text, lower):
        return True
    return False


def _is_user_script_produce(text: str, spec: dict[str, Any]) -> bool:
    """贴稿态下的「按原文出片」确认。放在 _is_iterate 之前,让"不用改/直接出视频"直达 PRODUCE。

    只在客户已贴了**自己的**完整文案(candidate_source=="user")时生效,避免误伤。
    """
    if spec.get("candidate_source") != "user" or not spec.get("candidate_script"):
        return False
    compact = re.sub(r"\s+", "", visible_user_text(text))
    return any(k in compact for k in (
        "不用改", "不修改", "不改了", "不用调整", "不需要改", "不需要修改", "无需修改",
        "别改", "不要改", "直接生成", "直接出视频", "直接出片", "直接出", "出视频",
        "就这样出", "照这个出", "按原文", "用这稿", "就用这个", "就这稿", "不用动",
    ))


def _has_produce_trigger(text: str, lower: str) -> bool:
    if any(k in text for k in ("提交", "执行", "开始吧", "开始生成", "生成吧", "开跑", "开始制作", "开始生产", "出片", "直接出视频", "出视频", "出吧", "走吧", "就这样", "这就行")):
        return True
    return any(k in lower for k in ("go", "ship", "submit", "produce", "generate", "start", "let's go", "do it", "send it", "make it"))


def script_acceptance_only(text: str) -> bool:
    visible = visible_user_text(text)
    return _is_script_acceptance(visible) and not _has_produce_trigger(visible, visible.lower())


def _is_script_acceptance(text: str) -> bool:
    return any(k in text for k in (
        "当成剧本", "当成脚本", "当作文案", "当作脚本", "当作剧本",
        "作为文案", "作为脚本", "作为剧本", "用作文案", "用作脚本", "用作剧本",
        "用我给你的", "这是我的剧本", "这是我的文案",
        # Compatibility with legacy mojibake strings still present in older tests/sessions.
        "褰撴垚鍓ф湰", "褰撴垚鑴氭湰", "褰撲綔鏂囨", "褰撲綔鑴氭湰",
        "浣滀负鏂囨", "浣滀负鑴氭湰", "鐢ㄤ綔鏂囨", "鐢ㄤ綔鑴氭湰",
        "鐢ㄦ垜缁欎綘", "浣犵敤鎴戠粰浣犵殑", "浣犲氨鎶婅繖涓",
    ))
