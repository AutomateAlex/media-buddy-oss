"""脚本锁 —— 架构的核心不变式。


TTS、素材下载、分镜、渲染都是花钱的入口。本模块提供一个"锁",
没锁定的脚本进不去这些阶段。

## 为什么需要它

验收闸本身已经排在这些阶段之前 —— 顺序是对的。锁要解决的是**另一个问题**:
以后新增的代码路径可能绕过闸(历史上已经发生过一次:"修复成功"那条出口
写成裸 return,直接绕过了长度保险丝)。**顺序靠约定,不变式靠守卫。**

## 旁路规则(必须有,否则卡死全部出片)

- `MB_SCRIPT_GATE=0`(整闸关闭):没人来锁,但也不该拦 → 自动置锁;
- `mode=="script"`(客户自带稿):我们不改也不拦,文责自负 → 自动置锁。

两种都记 `lock_source`,**不做静默旁路**(P6)—— 否则以后没人知道
某条片子当初为什么没过闸。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

LOCKED = "SCRIPT_LOCKED"
UNLOCKED = "SCRIPT_UNLOCKED"

# 这些 lock_source 表示"没过闸但放行了" —— 统计与排查时要能一眼区分。
BYPASS_SOURCES = frozenset({"gate_bypassed", "user_script", "gate_skipped"})

# 花钱的阶段。列在这里是为了让"哪些入口要守"这件事有单一出处。
PAYING_STAGES = ("tts", "footage", "chunking", "render")


class ScriptNotLockedError(RuntimeError):
    """脚本没锁就想进花钱阶段。"""


@dataclass
class ScriptLock:
    status: str = UNLOCKED
    lock_source: str = ""
    predicted_seconds: float = 0.0

    @property
    def is_locked(self) -> bool:
        return self.status == LOCKED

    @classmethod
    def locked(cls, *, source: str, predicted_seconds: float = 0.0) -> "ScriptLock":
        return cls(status=LOCKED, lock_source=source,
                   predicted_seconds=float(predicted_seconds or 0.0))

    @classmethod
    def unlocked(cls) -> "ScriptLock":
        return cls()


def lock_for_script(*, gate_result, mode: str, predicted_seconds: float) -> ScriptLock:
    """决定这条脚本以什么身份锁定。

    三种来源,都要留痕:
      gate_passed   —— 正常过闸
      gate_skipped  —— 闸没跑(早退路径),放行但如实标注
      gate_bypassed —— 整闸被关掉(MB_SCRIPT_GATE=0)
      user_script   —— 客户自带稿,我们不改也不拦
    """
    if str(mode or "") == "script":
        logger.info("script lock: user-supplied script, locking as user_script")
        return ScriptLock.locked(source="user_script",
                                 predicted_seconds=predicted_seconds)
    if os.environ.get("MB_SCRIPT_GATE", "1") == "0":
        logger.warning("script lock: gate disabled, auto-locking as gate_bypassed")
        return ScriptLock.locked(source="gate_bypassed",
                                 predicted_seconds=predicted_seconds)
    if gate_result is None:
        # 闸没跑(比如走了硬废稿 retry 那条早退路径)。**照放行,但如实说没跑** ——
        logger.warning("script lock: gate did not run; locking as gate_skipped")
        return ScriptLock.locked(source="gate_skipped",
                                 predicted_seconds=predicted_seconds)
    seconds = float(getattr(gate_result, "chosen_predicted_seconds", predicted_seconds) or 0.0)
    return ScriptLock.locked(source="gate_passed", predicted_seconds=seconds)


def ensure_locked(lock: Optional[ScriptLock], *, stage: str, project_id: str) -> None:
    """花钱阶段的入口守卫。**拿不到锁一律当没锁** —— "取不到就放行"等于这道闸不存在。

    `MB_SCRIPT_LOCK=0` 可整体旁路(紧急回滚开关:出片优先于纪律)。
    """
    if os.environ.get("MB_SCRIPT_LOCK", "1") == "0":
        return
    if lock is not None and lock.is_locked:
        return
    raise ScriptNotLockedError(
        f"script not locked for project={project_id}; stage '{stage}' refused. "
        "只有过了验收闸(或按旁路规则自动锁定)的脚本才能产生制作成本。"
    )
