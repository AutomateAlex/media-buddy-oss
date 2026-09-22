"""英文译稿的长度复检(Spec v2.1 §12)。

所有中文长度控制都作用在**中文稿**上,翻译发生在它们之后 —— 译文长度从没人量过。
影响面约 236 条/月(elevenlabs_amy,第二大音色)。

**本期只做粗控**:超过灾难线(1.25×)才动手,而且只动一次。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PURPOSE = "closure"

# **模型不听字数**。补偿到 0.82 ≈ 1/1.19。
TIGHTEN_OVERSHOOT_COMPENSATION = 0.82


def _tighten_prompt(target_words: int) -> str:
    target_words = max(20, int(target_words * TIGHTEN_OVERSHOOT_COMPENSATION))
    return (
        "This narration script is too long for its slot. Tighten the WORDING only.\n"
        f"Target: about {target_words} words.\n"
        "Rules:\n"
        "1. Do NOT drop any fact, name, date or number — compress phrasing instead.\n"
        "2. Keep the ending EXACTLY as it is: the ending is the payoff and it was "
        "deliberately crafted; removing it recreates the very bug we just fixed.\n"
        "3. Output only the tightened script, no commentary."
    )


def check_translated_script(
    text: str, *, duration_seconds, voice: str, speed, llm,
) -> tuple[str, bool]:
    """→ (最终译稿, 是否动过手)。**任何异常都返回原文** —— 不阻断出片。"""
    from backend.lib.tts_pacing import (
        count_words, english_length_verdict, english_target_words,
    )

    words = count_words(text)
    if english_length_verdict(words, voice, speed, duration_seconds) != "repair":
        return text, False

    target = english_target_words(duration_seconds, voice, speed)
    logger.warning(
        "english gate: translated script %d words is grossly over for %ss; "
        "one tightening pass to ~%d words", words, duration_seconds, target,
    )
    if llm is None:
        return text, False
    try:
        out = str(llm._call(_tighten_prompt(target), text, purpose=_PURPOSE) or "").strip()
    except Exception:  # noqa: BLE001 — 凝练失败照发原文
        logger.warning("english tightening failed; keeping translation", exc_info=True)
        return text, False
    if not out:
        return text, False

    out_words = count_words(out)
    if out_words >= words:
        # 压完反而更长 = 没听懂。退回原文,别把事情搞得更糟。
        logger.warning("english tightening returned %d words (was %d); keeping original",
                       out_words, words)
        return text, False

    # 复检:压完到底够不够?**不复检就永远不知道压没压够** ——
    after = english_length_verdict(out_words, voice, speed, duration_seconds)
    logger.info(
        "english gate: tightened %d → %d words (target %d); verdict now %s",
        words, out_words, target, after,
    )
    if after == "repair":
        logger.warning(
            "english gate: still over after one pass (%d words). 本期只压一次 —— "
            "英文没有定点修复能力,再压风险大于收益;已留痕待数据积累后评估。",
            out_words,
        )
    return out, True
