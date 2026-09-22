# -*- coding: utf-8 -*-
"""TTS 和取材并行（默认关）。

## 由来

两边都准备好之后再拼在一起」。

查过确实可行 —— 取材只吃 `script`（脚本文本），**不吃 `tts_path`**：

    _stage_assets_orchestrator(db, project_id, script, assets_dir, ...)

它们串行纯粹是代码顺序写死的，不是技术依赖。

## ⚠️ 但默认是关的，这不是偷懒

收益在「TTS 内部并行」落地后大幅缩水：

    并行前    TTS 13.6 分 + 取材 6.7 分 = 20.3 分
    TTS并行后  TTS 约 3 分 + 取材 6.7 分 =  9.7 分   ← 光这一步就省 10.6 分
    再叠本项   max(3, 6.7)             =  6.7 分   ← 只再省 3 分

单独验 TTS 内部并行，拿到真实数字再决定要不要打开这个开关。

## 这组守什么

1. 默认必须关（没验证过的东西不许自动生效）
2. 线程池所有退出路径都要关掉 —— 线程泄漏在 worker 里是累积的
3. 并行线程里不许共用外面的 DB session
4. 取材那段的逻辑没有被改动（只是搬了位置）
"""

from __future__ import annotations

import ast
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
PS = REPO / "src" / "backend" / "services" / "pipeline_service.py"


def _src() -> str:
    return io.open(PS, encoding="utf-8-sig").read()


def _fn(name: str) -> ast.FunctionDef:
    for n in ast.walk(ast.parse(_src())):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("找不到函数 %s" % name)


def test_默认必须是关的():
    """

    这个开关一旦默认打开，所有客户的出片流程立刻改变 ——
    而它带来的收益只有 3 分钟。

    ⚠️ 用 AST 取 `os.environ.get(KEY, 默认值)` 的**第二个实参**。
       第一版用 `src.index(KEY)` 往后截 200 字符再找 "0"，被上面那段
       注释里的 `MEDIA_BUDDY_PARALLEL_TTS_ASSETS=1 打开` 骗过去了 ——
       同一天在 TTS 那个文件上已经栽过一次。**守卫不许扫注释。**
    """
    default = None
    for n in ast.walk(ast.parse(_src())):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"):
            continue
        if not n.args or not isinstance(n.args[0], ast.Constant):
            continue
        if n.args[0].value != "MEDIA_BUDDY_PARALLEL_TTS_ASSETS":
            continue
        assert len(n.args) >= 2 and isinstance(n.args[1], ast.Constant), (
            "开关没有写默认值 —— 环境变量没设时行为不确定")
        default = str(n.args[1].value)
    assert default is not None, "找不到 MEDIA_BUDDY_PARALLEL_TTS_ASSETS 的读取"
    assert default.strip().lower() in ("0", "false", "no", "off", ""), (
        "TTS∥取材 的开关默认值是 %r —— 它只省 3 分钟却动了出片主流程，"
        "必须先单独验证过才能默认打开" % default)


def test_线程池的每条退出路径都要关掉():
    """🚨 线程泄漏在 worker 进程里是**累积**的 —— 每渲一条片子漏一个。

    用 AST 找出「启动线程池之后、取回结果之前」的所有 return/raise，
    每一条前面都必须有关闭动作。
    """
    fn = _fn("_stage_assets")
    src_lines = _src().splitlines()

    submit_at = None
    result_at = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "submit" and submit_at is None:
                submit_at = n.lineno
            if n.func.attr == "result" and result_at is None:
                result_at = n.lineno
    assert submit_at and result_at, "找不到线程池的提交/取回 —— 结构变了"

    risky = [n.lineno for n in ast.walk(fn)
             if isinstance(n, (ast.Return, ast.Raise))
             and submit_at < n.lineno < result_at]

    for ln in risky:
        # 往前看 3 行，必须有关闭动作
        window = "\n".join(src_lines[max(0, ln - 4): ln])
        assert "_close_assets_pool" in window or "shutdown" in window, (
            "第 %d 行有一条提前退出，但前面没关线程池 —— 每渲一条片子泄漏一个线程\n"
            "    %s" % (ln, src_lines[ln - 1].strip()))


def test_并行线程里必须自己开DB连接():
    """🚨 SQLAlchemy 的 session **不是线程安全的**，跨线程共用一个必炸。

    并行时 `db=None`，函数内部自己开；串行时才复用外面的。
    """
    fn = _fn("_run_assets_selection")
    body = ast.get_source_segment(_src(), fn) or ""
    assert "db is None" in body, (
        "没有「自己开 session」的分支 —— 并行时会和 TTS 共用一个 session")
    assert "SessionLocal()" in body, "找不到自己开 session 的代码"
    assert "close()" in body, "自己开的 session 没关 —— 连接池会被耗干"


def test_取材逻辑没被改动只是搬了位置():
    """📌 抽函数的目的只有一个：能丢进线程里。

    逻辑一旦被顺手改了，出问题时就分不清是「并行」还是「改动」导致的。
    """
    body = ast.get_source_segment(_src(), _fn("_run_assets_selection")) or ""
    for must in (
        "_stage_assets_orchestrator",
        "_raise_if_stopped",
        "_write_stage_progress",
    ):
        assert must in body, (
            "搬运时弄丢了 `%s` —— 抽函数只该换位置，不该改逻辑" % must)


def test_串行路径必须还在():
    """⚠️ 开关关着的时候，必须走原来那条路，一模一样。"""
    fn = _fn("_stage_assets")
    body = ast.get_source_segment(_src(), fn) or ""
    assert "_assets_future is not None" in body, (
        "没有「开关关着走串行」的分支 —— 那样开关就形同虚设")
    assert "_run_assets_selection(" in body, "串行路径没有调用取材"
