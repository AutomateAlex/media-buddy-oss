"""Style presets — visual identity locks for Series-level brand consistency.

Phase 2.11k — when a Series picks a style preset, the LLM's ``plan_chunks``
output gets the preset's ``style_block`` and ``constraints_block`` injected
into every per-chunk ``ai_prompt`` (replacing what the LLM would write
for those two blocks). LLM still freely writes Subject / Action / Camera
— the visually variable parts that differ per shot.

This guarantees: across all videos in the same Series, the look (lighting,
grain, color treatment) and the bans (no cartoon / no logos / etc.) stay
identical, while the subject and motion adapt per chunk.

Each preset is a dict with:
  id          — stable string key, used in DB / API
  label_zh    — Chinese label for the UI dropdown
  description — one-line guidance ("when to pick this")
  style_block — the Style: line content (no leading "Style:")
  constraints_block — the Constraints: line content (no leading "Constraints:")
  thumbnail_emoji — quick visual hint in the picker (no real assets needed)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StylePreset:
    id: str
    label_zh: str
    description: str
    style_block: str
    constraints_block: str
    thumbnail_emoji: str = ""


STYLE_PRESETS: dict[str, StylePreset] = {
    p.id: p for p in [
        StylePreset(
            id="documentary_natural",
            label_zh="纪实自然 (BBC 风)",
            description="自然光纪录片感，适合科普 / 自然 / 历史 / 知识类",
            style_block=(
                "BBC nature documentary look, golden hour or soft daylight, "
                "natural color palette, subtle film grain, gentle contrast"
            ),
            constraints_block=(
                "no cartoon, no surreal elements, no captions, no UI overlays, "
                "no extreme close-ups of faces, hold steady, 5-7s"
            ),
            thumbnail_emoji="🦒",
        ),
        StylePreset(
            id="cinematic_warm",
            label_zh="电影感暖调",
            description="35mm 浅景深 + 温暖色调，适合故事 / 情感 / 品牌叙事",
            style_block=(
                "Cinematic 35mm anamorphic look, golden hour warm light, "
                "amber-and-teal grading, shallow depth of field, soft bokeh, "
                "fine film grain"
            ),
            constraints_block=(
                "no digital sharpness, no neon, no UI overlays, no captions, "
                "no jump cuts, hold steady, 5-7s"
            ),
            thumbnail_emoji="🎬",
        ),
        StylePreset(
            id="clean_studio",
            label_zh="干净商业棚拍",
            description="白底 + 柔光箱，适合产品介绍 / 商业广告",
            style_block=(
                "Clean commercial studio, soft white seamless backdrop, large "
                "softbox key light, subtle rim light, crisp digital sharpness, "
                "neutral white balance"
            ),
            constraints_block=(
                "no outdoor scenes, no clutter, no captions, no logos, no "
                "extra hands, no surreal elements, hold steady, 5-6s"
            ),
            thumbnail_emoji="📦",
        ),
        StylePreset(
            id="vlog_handheld",
            label_zh="Vlog 手持自然",
            description="手持轻微晃动 + 室内自然光，适合个人频道 / 日常 / 记录",
            style_block=(
                "Vlog handheld phone perspective, slight natural sway, indoor "
                "ambient daylight, ungraded look, light motion blur, casual "
                "framing"
            ),
            constraints_block=(
                "no cinematic gimbal smoothness, no studio lighting, no "
                "captions, no UI overlays, natural movements only, 6-8s"
            ),
            thumbnail_emoji="📱",
        ),
        StylePreset(
            id="food_macro",
            label_zh="美食微距",
            description="顶光 + 微距特写 + 蒸汽质感，适合美食 / 烹饪 / 饮品",
            style_block=(
                "Food macro photography, overhead or low-angle close-up, soft "
                "directional window light, shallow depth of field, rich warm "
                "tones, gentle steam visible, subtle film grain"
            ),
            constraints_block=(
                "no wide establishing shots, no faces, no captions, no logos, "
                "no surreal saturation, hold steady, 4-6s"
            ),
            thumbnail_emoji="🍜",
        ),
        StylePreset(
            id="science_explainer",
            label_zh="科普讲解",
            description="干净背景 + 中景 + 标准镜头，适合教程 / 知识 / 科普",
            style_block=(
                "Clean educational explainer, neutral grey or pale studio "
                "backdrop, soft even lighting, medium-shot framing, balanced "
                "natural colors, crisp digital clarity"
            ),
            constraints_block=(
                "no dramatic shadows, no shallow depth, no surreal effects, "
                "no captions, no UI overlays, hold steady, 5-6s"
            ),
            thumbnail_emoji="🔬",
        ),
        StylePreset(
            id="nature_wild",
            label_zh="自然野生 (长焦)",
            description="长焦 + 黄金时刻 + 生态摄影，适合野生动物 / 风光 / 旅行",
            style_block=(
                "Wildlife and nature photography, long telephoto lens 200mm, "
                "golden hour rim light, deep saturated greens and earth tones, "
                "shallow depth isolating subject, ambient natural soundscape "
                "feel"
            ),
            constraints_block=(
                "no urban elements, no captions, no logos, no surreal effects, "
                "no zoom artifacts, hold steady, 5-7s"
            ),
            thumbnail_emoji="🦅",
        ),
        StylePreset(
            id="history_archival",
            label_zh="老胶片档案感",
            description="16mm 颗粒 + 暖橙色 + 暗角，适合历史 / 怀旧 / 纪念",
            style_block=(
                "Vintage 16mm archival film texture, warm amber and sepia "
                "tones, heavy film grain, gentle vignette, slight color fade, "
                "soft natural light"
            ),
            constraints_block=(
                "no modern phones, no plastic objects, no neon signs, no "
                "digital sharpness, no captions, no UI overlays, hold steady, 5-7s"
            ),
            thumbnail_emoji="📽️",
        ),
        StylePreset(
            id="kids_bright",
            label_zh="儿童明亮",
            description="高饱和暖色 + 柔光 + 温馨，适合儿童 / 教育 / 卡通向但不卡通",
            style_block=(
                "Bright cheerful warm palette, soft even daylight, mid "
                "saturation, friendly inviting tone, balanced framing, gentle "
                "contrast"
            ),
            constraints_block=(
                "no dark scenes, no scary elements, no violence, no weapons, "
                "no surreal distortions, no captions, hold steady, 5-7s"
            ),
            thumbnail_emoji="🌈",
        ),
        StylePreset(
            id="tech_minimal",
            label_zh="科技极简",
            description="冷光 + 几何构图 + 微距特写，适合科技产品 / 数码 / 工业",
            style_block=(
                "Minimalist tech aesthetic, cool grey palette with single "
                "accent color, hard rim light, geometric composition, macro "
                "details, clean digital sharpness, slight reflective surfaces"
            ),
            constraints_block=(
                "no cluttered backgrounds, no warm tones, no nature elements, "
                "no captions, no logos, hold steady, 4-6s"
            ),
            thumbnail_emoji="⚙️",
        ),
    ]
}


def get_preset(preset_id: str | None) -> StylePreset | None:
    if not preset_id:
        return None
    return STYLE_PRESETS.get(preset_id)


def list_presets() -> list[dict]:
    """Serializable list for API/UI consumption."""
    return [
        {
            "id": p.id,
            "label_zh": p.label_zh,
            "description": p.description,
            "style_block": p.style_block,
            "constraints_block": p.constraints_block,
            "thumbnail_emoji": p.thumbnail_emoji,
        }
        for p in STYLE_PRESETS.values()
    ]
