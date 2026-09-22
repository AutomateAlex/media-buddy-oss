"""SCRIPT_LOCKED —— 架构的核心不变式(Spec v2.1 §10)。


TTS、素材下载、分镜、渲染都是花钱的入口。没锁定的脚本进不去。

⚠️ 旁路规则是必须的,不写会**卡死全部出片**:
  - `MB_SCRIPT_GATE=0`(整闸关闭)→ 没人来锁,但也不该拦;
  - `mode=="script"`(客户自带稿)→ 我们不改也不该拦,文责自负。
两种情况都自动置锁,并用 `lock_source` 记下是怎么锁上的。
"""
import pytest


def test_lock_states_are_explicit():
    from backend.lib.script_lock import LOCKED, ScriptLock

    lock = ScriptLock.locked(source="gate_passed", predicted_seconds=57.0)
    assert lock.status == LOCKED
    assert lock.lock_source == "gate_passed"
    assert lock.predicted_seconds == 57.0


def test_unlocked_script_cannot_enter_a_paying_stage():
    from backend.lib.script_lock import ScriptLock, ensure_locked, ScriptNotLockedError

    with pytest.raises(ScriptNotLockedError):
        ensure_locked(ScriptLock.unlocked(), stage="tts", project_id="p1")


def test_locked_script_passes_every_paying_stage():
    from backend.lib.script_lock import ScriptLock, ensure_locked

    lock = ScriptLock.locked(source="gate_passed", predicted_seconds=57.0)
    for stage in ("tts", "footage", "chunking", "render"):
        ensure_locked(lock, stage=stage, project_id="p1")   # 不抛即通过


def test_missing_lock_object_is_treated_as_unlocked():
    """拿不到锁 = 没锁。绝不能"取不到就放行"——那等于这道闸不存在。"""
    from backend.lib.script_lock import ensure_locked, ScriptNotLockedError

    with pytest.raises(ScriptNotLockedError):
        ensure_locked(None, stage="tts", project_id="p1")


# ── 旁路规则:不写会卡死全部出片 ────────────────────────────────────

def test_gate_disabled_auto_locks_so_production_is_not_blocked(monkeypatch):
    monkeypatch.setenv("MB_SCRIPT_GATE", "0")
    from backend.lib.script_lock import lock_for_script

    lock = lock_for_script(gate_result=None, mode="creative", predicted_seconds=0.0)

    assert lock.is_locked
    assert lock.lock_source == "gate_bypassed"


def test_user_supplied_script_auto_locks_with_its_own_source():
    """客户自带稿:我们不改、也不拦,但要记下来 —— 文责自负,可追溯。"""
    from backend.lib.script_lock import lock_for_script

    lock = lock_for_script(gate_result=None, mode="script", predicted_seconds=48.0)

    assert lock.is_locked
    assert lock.lock_source == "user_script"


def test_gate_passed_script_locks_with_the_gate_source():
    from backend.lib.script_lock import lock_for_script

    class Gate:
        chosen_predicted_seconds = 57.2
        chosen_version = "v2"

    lock = lock_for_script(gate_result=Gate(), mode="creative", predicted_seconds=57.2)

    assert lock.is_locked
    assert lock.lock_source == "gate_passed"
    assert lock.predicted_seconds == 57.2


def test_every_bypass_is_recorded_not_silent():
    """P6:旁路也要留痕。静默旁路 = 以后没人知道这条片子为什么没过闸。"""
    from backend.lib.script_lock import BYPASS_SOURCES

    assert "gate_bypassed" in BYPASS_SOURCES
    assert "user_script" in BYPASS_SOURCES
    assert "gate_passed" not in BYPASS_SOURCES


def test_lock_can_be_switched_off_for_emergencies(monkeypatch):
    """回滚开关:MB_SCRIPT_LOCK=0 → 守卫整体旁路(出片优先于纪律)。"""
    monkeypatch.setenv("MB_SCRIPT_LOCK", "0")
    from backend.lib.script_lock import ensure_locked

    ensure_locked(None, stage="tts", project_id="p1")   # 不抛


# ── 回归:闸没跑,锁不许说"过闸了" ──────────────────────────────────

def test_lock_source_tells_the_truth_when_the_gate_never_ran():
    """
    但锁照样标 `gate_passed` —— 锁在说谎,事后查数据会被彻底误导。

    闸没跑就该说没跑。
    """
    from backend.lib.script_lock import lock_for_script

    lock = lock_for_script(gate_result=None, mode="creative", predicted_seconds=0.0)

    assert lock.is_locked                       # 仍然放行(不阻断出片)
    assert lock.lock_source == "gate_skipped", "闸没跑就不许标 gate_passed"


def test_gate_skipped_counts_as_a_bypass_for_statistics():
    from backend.lib.script_lock import BYPASS_SOURCES

    assert "gate_skipped" in BYPASS_SOURCES
