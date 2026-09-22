"""Multimodal Omni client — judges video clips against sentences.

Two providers, switch via OMNI_PROVIDER env var:
- openrouter: MiMo-V2-Omni via OpenRouter. Keyframe-based fallback (most

Score schema returned by score_clip():
    {
      "score": 0-10 (overall),
      "visual_match": 0-10 (subject/action match the sentence),
      "pacing": 0-10 (clip duration suits the line),
      "mood": 0-10 (emotional tone fits),
      "composition": 0-10 (shot quality),
      "issues": [str, ...],
      "suggestion": str,
      "provider": str,
    }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Common Windows install paths checked when ffmpeg/ffprobe aren't on PATH
_FFMPEG_FALLBACK_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
]


def _find_bin(name: str) -> str:
    """Resolve ffmpeg/ffprobe path. Order: env var override → PATH → common dirs."""
    env_key = f"{name.upper()}_BIN"
    if v := os.environ.get(env_key):
        if Path(v).exists():
            return v
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    if found:
        return found
    for d in _FFMPEG_FALLBACK_DIRS:
        for ext in ("", ".exe"):
            candidate = Path(d) / f"{name}{ext}"
            if candidate.exists():
                return str(candidate)
    return name  # let subprocess raise FileNotFoundError with the bare name


# Recognized moderation flag taxonomy. Models are instructed to use
# exactly these keys so downstream code can switch on them.
# users want full freedom for weapon display. Real weapon violence falls
# under `violence` instead.
MODERATION_FLAGS = [
    "nudity",              # full or partial nudity
    "sexual_suggestive",   # sexual posing, suggestive but no nudity
    "violence",            # real fights, blood drawn, weapons used to harm
    "gore",                # severe injury, mutilation, corpses
    "minors",              # minors in unsafe / inappropriate context
    "drugs",               # drug use / paraphernalia
    "alcohol_cigarettes",  # alcohol or smoking visible
    "hate",                # hate symbols / racist imagery
    "illegal",             # other clearly illegal activity
]


MODERATION_SYSTEM_PROMPT = (
    "你是专业内容审核员。会收到 1 段短视频帧。"
    "判断画面是否含违规内容，输出 JSON。\n\n"
    "⚠️ 重要原则（先理解再判断）：\n"
    "1. 武器（枪支 / 刀具 / 弓箭 / 任何兵器）单纯出现 = ok。\n"
    "   持握、瞄准、特写、慢动作、戏剧化打光、训练、展示 → 全部 ok。\n"
    "   **唯一例外**：武器在攻击/威胁活生生的人 → 归类到 violence。\n"
    "2. 体育对抗（拳击 / MMA / 散打 / 击剑 / 武术）= ok。\n"
    "3. 标签数量 ≠ 严苛度。看到边界画面优先 ok / warn，不要凑标签。\n\n"
    "标签定义（严格按此判定，避免误伤）：\n"
    "- nudity: 全裸 / 半裸 / 暴露生殖器或胸部 / 内衣单独出镜以暴露身体为重点\n"
    "    ✓ block: 上身赤裸（无论性别）/ 仅穿内衣或比基尼且画面聚焦身体\n"
    "    ✗ ok: 短袖短裤运动装 / 泳池有人穿泳衣但只是远景活动\n"
    "- sexual_suggestive: 性挑逗姿势 / 暗示性接触（不裸）\n"
    "    ✓ block: 抚摸性感部位 / 性化角度拍摄 / 亲密接触暗示性活动\n"
    "    ✗ ok: 普通拥抱 / 接吻镜头 / 情侣手牵手\n"
    "- violence: 真实施害行为，含血或带伤害意图\n"
    "    ✓ block: 街头斗殴 / 真实袭击 / 出血伤亡 / 用武器威胁或攻击他人\n"
    "    ✗ ok: **拳击 / MMA / 散打 / 击剑 / 武术对抗等体育竞技**\n"
    "    ✗ ok: **动作片武术编排 / 训练场景 / 历史战争纪录片**\n"
    "- gore: 严重血腥 / 残肢 / 尸体 / 内脏\n"
    "    ✓ block: 任何明显血腥镜头\n"
    "    ✗ ok: 医学手术（教育用途）/ 影视化妆血\n"
    "- minors: 未成年人在不当场景中（成人话题 / 危险环境 / 任何性化）\n"
    "    ✓ block: 任何儿童相关疑似不当场景\n"
    "- drugs: 吸毒 / 毒品器具明显\n"
    "- alcohol_cigarettes: 酒精饮用 / 抽烟明显（kids 行业才严管）\n"
    "- hate: 仇恨符号 / 种族歧视\n"
    "- illegal: 其他明显违法（盗窃/纵火/明显毒品交易）\n\n"
    "判断 severity 时谨慎，避免过度拦截：\n"
    "- ok: 无违规，正常使用\n"
    "- warn: 边界内容（如夜间饮酒画面、历史档案的轻度对抗），可用但标记\n"
    "- block: 必须拦截（裸体 / 性化 / 严重血腥 / 涉童 / 戏剧化武器美化）\n\n"
    "重要：客户的 forbidden_topics 列表只是过滤器，**不要看到武器/对抗就一律 block**。\n"
    "先按上面定义判断画面属于哪一类，再对照 forbidden_topics 决定是否拦截。\n"
    "比如客户禁止 weapon_glorify，但画面是厨房菜刀切菜 → 不是 weapon_glorify → ok。\n\n"
    "严格输出 JSON：{\"is_safe\":bool, \"flags\":[\"...\"], "
    "\"severity\":\"ok|warn|block\", \"confidence\":0.0-1.0, \"reason\":\"...\"}"
)


SYSTEM_PROMPT = (
    "你是专业视频剪辑师，给短视频选 B-roll。会收到 1 段短视频 + 1 句旁白 + "
    "脚本的文化归属。**像真人剪辑师那样判断**：画面能不能跟旁白对得上戏，"
    "用 5 个独立维度评分（0-10，整数；不要五个维度都给同一个分数）。\n\n"
    "⚠️ 第一原则：**culture_match 是一票否决**\n"
    "如果脚本文化是 France/China/Japan/Korea/Italy/Spain/USA/UK 中的某一个，"
    "而画面里出现明显的对立文化元素（例：脚本是 France 但画面里有中式寺庙 / "
    "中文招牌 / 日式神社 / 旗袍 / 和服），culture_match 必须给 0-2 分，且 "
    "decision 直接 reject，不管 visual_match 多高。文化错位的镜头比构图差的"
    "镜头更致命——观众会立刻看出违和感。\n\n"
    "⚠️ 第二原则：**视觉连贯性 > 字面匹配（仅在文化对的前提下）**\n"
    "1. 文化对了，再判断字面。真人剪辑遇到「字面上画面不存在」时（旁白说"
    "『鳄鱼用浮木做游行船』），会用**同主题的普通镜头**填补——"
    "只要观众看到『啊，是鳄鱼』就成立。\n"
    "2. 因此，奇幻/夸张/比喻句子搭配文化正确的真实素材，**主体+场景对**，"
    "就给 visual_match 7-9 分。\n"
    "3. 扣分情况：\n"
    "   - 主体完全错（说鳄鱼配狮子）→ visual_match 2-4\n"
    "   - 文化错（脚本是法国，画面是中国）→ culture_match 0-2 + reject\n"
    "   - 画面残缺/抖动/超暗 → composition 低\n"
    "   - 画面里有不该有的元素 → visual_match 3-5\n\n"
    "评分维度（5 个，每个 0-10 整数，独立打分，不要全给同一个分数）：\n"
    "- visual_match: 画面**主体+场景**是否在旁白的『语义邻里』\n"
    "- culture_match: 画面文化背景是否与脚本文化一致\n"
    "    例：脚本 France + 画面巴黎咖啡馆 → 9-10\n"
    "    例：脚本 France + 画面中性日落 → 5-6（中性不扣狠）\n"
    "    例：脚本 France + 画面南京街景/中国寺庙 → 0-2（一票否决）\n"
    "    例：脚本 Universal → 一律 5+（脚本无文化要求，画面无所谓）\n"
    "- pacing: 镜头时长 / 节奏与文案是否搭\n"
    "- mood: 情绪基调一致性（紧张句子配静谧画面 → 扣分）\n"
    "- composition: 取景、构图、清晰度、稳定性\n"
    "- score: 综合判断（5 维度加权，culture_match 权重最高）\n"
    "  如果 culture_match ≤ 2，score 必须 ≤ 3 并 reject。\n\n"
    "严格输出 JSON："
    "{\"score\":N,\"visual_match\":N,\"culture_match\":N,\"pacing\":N,\"mood\":N,"
    "\"composition\":N,\"issues\":[\"...\"],\"suggestion\":\"...\","
    "\"culture_reason\":\"<one-sentence English reason for culture_match score>\"}"
)


def _build_user_prompt(
    sentence: str,
    context_prev: str,
    context_next: str,
    *,
    culture: str = "Universal",
) -> str:
    parts: list[str] = []
    parts.append(f"脚本文化（culture）：{culture}")
    if context_prev:
        parts.append(f"上一句：{context_prev}")
    parts.append(f"当前旁白：{sentence}")
    if context_next:
        parts.append(f"下一句：{context_next}")
    parts.append(
        "把自己当真人剪辑师：这块视频能不能作为这段旁白的 B-roll？\n"
        "1. 先判断 culture_match：画面文化是否对得上脚本文化。文化错位 → "
        "culture_match 0-2 + reject，无论 visual_match 多高。\n"
        "2. 文化对了再看 visual_match：主体+场景对就算通过，不强求字面对应。\n"
        "按 5 维度独立评分（不要全给同一个分数），仅返回 JSON。"
    )
    return "\n".join(parts)


def _parse_score_json(text: str, provider: str) -> dict[str, Any]:
    """Robust JSON extraction — model may wrap in markdown fences or add prose."""
    if isinstance(text, list):
        text = "".join(c.get("text", "") for c in text if isinstance(c, dict))
    text = text.strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON in omni response: {text[:200]}")
        d = json.loads(m.group(0))

    for k in ("score", "visual_match", "culture_match", "pacing", "mood", "composition"):
        try:
            d[k] = float(d.get(k, 0))
        except (TypeError, ValueError):
            d[k] = 0.0
    # Backfill culture_match when older models / cached responses don't have it.
    # Defaulting to 5.0 (neutral) is safer than 0 (would auto-reject everything).
    if "culture_match" not in d or d["culture_match"] == 0:
        # Only force-default to 5 if it's clearly missing (not set to 0 by an
        # actual culture-fail judgment, which we want to honor). Heuristic:
        # if model didn't emit the key at all OR emitted exactly 0.0 and
        # didn't write a culture_reason, treat as missing → neutral 5.
        if not d.get("culture_reason"):
            d["culture_match"] = 5.0
    d.setdefault("issues", [])
    d.setdefault("suggestion", "")
    d.setdefault("culture_reason", "")
    d["provider"] = provider
    return d


def _parse_moderation_json(text: str, provider: str) -> dict[str, Any]:
    """Parse moderation JSON. Defensively coerce fields + filter unknown flags.

    Critical rule: if `flags` is empty after filtering against MODERATION_FLAGS
    taxonomy, severity is forced to `ok`. This prevents model bias from
    blocking content with no concrete violation (we observed Qwen3-VL
    flagging weapon close-ups as block with empty flags despite explicit
    prompt instructions). The taxonomy is the source of truth for what
    constitutes a violation.
    """
    if isinstance(text, list):
        text = "".join(c.get("text", "") for c in text if isinstance(c, dict))
    text = text.strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON in moderation response: {text[:200]}")
        d = json.loads(m.group(0))
    severity = str(d.get("severity", "warn")).lower()
    if severity not in ("ok", "warn", "block"):
        severity = "warn"
    flags = [f for f in d.get("flags", []) if f in MODERATION_FLAGS]
    # Enforce: no concrete recognized flag → severity must be ok.
    if not flags and severity != "ok":
        severity = "ok"
    is_safe = bool(d.get("is_safe", severity == "ok"))
    try:
        confidence = float(d.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    return {
        "is_safe": is_safe,
        "flags": flags,
        "severity": severity,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(d.get("reason", ""))[:300],
        "provider": provider,
    }


def _build_moderation_user_prompt(
    forbidden_topics: list[str] | None, industry: str,
) -> str:
    parts = ["请审核此画面是否含违规内容。"]
    if industry:
        parts.append(f"内容行业类别：{industry}（按此调整严苛度）。")
    if forbidden_topics:
        parts.append(f"客户特别禁止：{', '.join(forbidden_topics)}。")
    parts.append("仅返回 JSON。")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Phase 2.9a — Library tagging
# ----------------------------------------------------------------------

# Controlled vocabulary the LLM picks from. Mirrors LibraryService.CATEGORIES.
LIBRARY_CATEGORIES = [
    "people", "nature", "urban", "interior", "food",
    "business", "abstract", "tech", "sports",
    "vehicles", "animals", "textures", "other",
]

TAGGING_SYSTEM_PROMPT = (
    "You are a stock-footage librarian. Look at the video frame and produce "
    "structured indexing tags so the same clip can be found later by keyword "
    "search across many videos.\n\n"
    "Output STRICT JSON with these keys:\n"
    "  - category:     ONE of [" + ", ".join(LIBRARY_CATEGORIES) + "]\n"
    "  - tags:         5-15 lowercase English keywords (subject + setting + "
    "objects + style). Be specific: \"barista\", \"coffee\", \"cafe\", \"steam\" "
    "instead of \"morning\". No abstract concepts.\n"
    "  - description:  ONE sentence in English describing what the camera "
    "would see. 15-30 words. Concrete, factual, no editorializing.\n"
    "  - mood_tags:    1-4 mood/atmosphere words from a fixed-ish set "
    "[calm, warm, energetic, tense, intimate, dramatic, melancholic, playful, "
    "mysterious, hopeful, somber, uplifting, neutral]\n"
    "  - motion_tags:  1-4 motion descriptors from "
    "[static, slow_pan, fast_pan, zoom_in, zoom_out, dolly, handheld, "
    "drone_shot, tracking_shot, time_lapse, slow_motion, hand_motion, "
    "liquid_pour, walking, running, vehicle_motion, no_motion]\n"
    "Be HONEST: if the frame is a still product shot, say motion_tags=[\"static\"]. "
    "Output ONLY the JSON object."
)


def _build_tagging_user_prompt() -> str:
    return (
        "Index this clip for a stock-library search index. "
        "Return ONLY the JSON object (no markdown fences)."
    )


def _parse_tagging_json(text: str, provider: str) -> dict[str, Any]:
    """Robust parse + validate. Coerces unknown category to 'other'."""
    if isinstance(text, list):
        text = "".join(c.get("text", "") for c in text if isinstance(c, dict))
    text = text.strip()
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON in tagging response: {text[:200]}")
        d = json.loads(m.group(0))

    cat = str(d.get("category", "")).strip().lower()
    if cat not in LIBRARY_CATEGORIES:
        cat = "other"

    def _list_of_str(key: str, max_n: int) -> list[str]:
        raw = d.get(key, []) or []
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip().lower())
        return out[:max_n]

    return {
        "category":    cat,
        "tags":        _list_of_str("tags", 15),
        "description": str(d.get("description", ""))[:500],
        "mood_tags":   _list_of_str("mood_tags", 4),
        "motion_tags": _list_of_str("motion_tags", 4),
        "provider":    provider,
    }


class OmniClient(ABC):
    """Abstract base for multimodal video critics."""

    name: str = "omni"

    @abstractmethod
    def score_clip(
        self,
        clip_path: str,
        sentence: str,
        context_prev: str = "",
        context_next: str = "",
        *,
        culture: str = "Universal",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def moderate_clip(
        self,
        clip_path: str,
        forbidden_topics: list[str] | None = None,
        industry: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def tag_clip(self, clip_path: str) -> dict[str, Any]: ...

    def is_available(self) -> bool:
        return False


class OpenRouterOmniClient(OmniClient):
    """

    (system + user with text + image_url content) is routed through the
    multimodal content shape (cloud commit 39afae9). Cloud holds the master
    OpenRouter key and applies quota / subscription gates.
    """

    name = "openrouter"
    # structured-JSON output. Env override `OMNI_MODEL` still wins.
    DEFAULT_MODEL = "google/gemini-2.5-flash"

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("OMNI_MODEL") or self.DEFAULT_MODEL
        self.project_id: Optional[str] = None

    def set_project_id(self, project_id: Optional[str]) -> None:
        self.project_id = project_id

    def _gateway(self):
        """Returns the process-wide CloudGateway singleton.

        See cloud_auth.get_default_auth() for why this must be a singleton
        (refresh-token rotation race under concurrency).
        """
        from backend.lib.cloud_gateway import get_default_gateway
        return get_default_gateway()

    def is_available(self) -> bool:
        try:
            return self._gateway().is_authenticated()
        except Exception as e:  # defensive
            logger.debug("omni openrouter availability check failed: %s", e)
            return False

    def _llm_call(
        self,
        system_prompt: str,
        user_text: str,
        frame_b64: str,
        max_tokens: int,
    ) -> str:
        """Common multimodal completion via cloud gateway."""
        import asyncio
        import uuid

        from backend.lib.cloud_auth import NotAuthenticatedError
        from backend.lib.cloud_gateway import (
            QuotaExceededError,
            SubscriptionInactiveError,
        )

        gw = self._gateway()
        if not gw.is_authenticated():
            raise RuntimeError("cloud not authenticated; user must pair desktop")
        try:
            result = asyncio.run(gw.llm_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{frame_b64}",
                                },
                            },
                        ],
                    },
                ],
                idempotency_key=str(uuid.uuid4()),
                max_tokens=max_tokens,
                model_id=self.model,
                project_id=self.project_id,
            ))
        except NotAuthenticatedError as e:
            raise RuntimeError(f"cloud not authenticated: {e}") from e
        except QuotaExceededError as e:
            raise RuntimeError(str(e)) from e
        except SubscriptionInactiveError as e:
            raise RuntimeError(f"subscription inactive: {e.payload}") from e
        return result["content"]

    def score_clip(self, clip_path, sentence, context_prev="", context_next="", *, culture="Universal"):
        if not self.is_available():
            raise RuntimeError("cloud not authenticated; user must pair desktop")
        frame_b64 = _extract_middle_frame_b64(clip_path)
        text = self._llm_call(
            SYSTEM_PROMPT,
            _build_user_prompt(
                sentence, context_prev, context_next, culture=culture,
            ),
            frame_b64,
            max_tokens=800,
        )
        return _parse_score_json(text, self.name)

    def moderate_clip(self, clip_path, forbidden_topics=None, industry=""):
        if not self.is_available():
            raise RuntimeError("cloud not authenticated; user must pair desktop")
        frame_b64 = _extract_middle_frame_b64(clip_path)
        text = self._llm_call(
            MODERATION_SYSTEM_PROMPT,
            _build_moderation_user_prompt(forbidden_topics, industry),
            frame_b64,
            max_tokens=500,
        )
        return _parse_moderation_json(text, self.name)

    def tag_clip(self, clip_path):
        if not self.is_available():
            raise RuntimeError("cloud not authenticated; user must pair desktop")
        frame_b64 = _extract_middle_frame_b64(clip_path)
        text = self._llm_call(
            TAGGING_SYSTEM_PROMPT,
            _build_tagging_user_prompt(),
            frame_b64,
            max_tokens=800,
        )
        return _parse_tagging_json(text, self.name)


def _extract_middle_frame_b64(clip_path) -> str:
    """Use ffmpeg to grab the frame at clip's midpoint, return base64 JPEG."""
    ffprobe = _find_bin("ffprobe")
    ffmpeg = _find_bin("ffmpeg")
    clip = Path(clip_path)
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
        capture_output=True, text=True, timeout=10,
        encoding="utf-8", errors="replace",
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        duration = 5.0
    midpoint = max(0.1, duration / 2.0)

    fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-ss", str(midpoint), "-i", str(clip),
             "-vframes", "1", "-q:v", "3", tmp_path],
            capture_output=True, timeout=30,
        )
        with open(tmp_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_omni_client(provider: Optional[str] = None) -> OmniClient:
    """OpenRouter (through the local gateway) is the only vision backend in this build."""
    return OpenRouterOmniClient()
