# -*- coding: utf-8 -*-
"""

## 客户会怎么被坑

客户用**中文界面**，把出片语言选成英文。字幕开关是开的，字幕语言却还停在
默认的**简体中文** —— 因为旧默认值只看界面语言。

客户忘了手动去改 → 整条英文片底下跑中文字幕 → **片子废掉** → 来找我们退钱。
批量生产更狠：一次几十条，一起废。

## 规则

**只在字幕里压根没有目标语言时才动它**：

    出英文 + 字幕纯中文   → 换成 en
    出中文 + 字幕纯英文   → 换回界面对应的中文（繁体界面给繁体）
    双语 zh-*+en          → 两个方向都不动（客户特意挑的）

## 两半都要有，缺一半就还会退钱

1. **切换出片语言的那一刻**联动改字幕（四个入口都要）
2. **提交/默认值那一层的兜底**也要看出片语言 ——
   存过频道默认值的老客户根本不会触发任何 onChange，光改第 1 条漏得掉

⚠️ 前端没有测试运行器，所以这里用 node + typescript 把那两个纯函数
   **真跑一遍**，而不是只在源码里搜字符串。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

FE = Path(__file__).resolve().parents[2] / "src/frontend"
I18N = FE / "src/i18n/index.ts"
WORKSPACE = FE / "src/pages/WorkspacePages.tsx"
CHANNELS = FE / "src/pages/Channels.tsx"
RADAR_DLG = FE / "src/features/radar/components/BatchSettingsDialog.tsx"


# ────────────────────────── 真跑一遍那两个纯函数 ──────────────────────────

def _extract(name: str, src: str) -> str:
    """从 index.ts 里抠出一个顶层 export function 的完整源码（按大括号配平）。"""
    m = re.search(r"^export function %s\b" % re.escape(name), src, re.M)
    assert m, "找不到 %s —— 是不是被改名/删了？" % name
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError("%s 的大括号没配平" % name)


def _run_helper(ui_lang: str, cases: list) -> list:
    """在 node 里跑真实的 helper。`subtitleLanguageFromUi` 用桩替掉（它依赖 i18next）。"""
    ts = pytest.importorskip  # 占位，避免 lint 抱怨；实际不用
    src = I18N.read_text(encoding="utf-8")
    body = "\n\n".join([
        _extract("effectiveSubtitleLanguage", src),
        _extract("subtitleLanguageForOutput", src),
    ])
    stub = (
        "const UI = %s;\n"
        "export function subtitleLanguageFromUi(): any {\n"
        "  return UI === 'zh-Hant' ? 'zh-Hant' : UI.startsWith('zh') ? 'zh-Hans' : 'en'\n"
        "}\n"
        "type SubtitleLangValue = string\n"
    ) % json.dumps(ui_lang)
    driver = (
        "\nconst CASES: any[] = %s;\n"
        "const out = CASES.map((c: any) => ({\n"
        "  follow: subtitleLanguageForOutput(c.output, c.current) ?? null,\n"
        "  effective: effectiveSubtitleLanguage(c.current, c.output),\n"
        "}));\n"
        "console.log('@@' + JSON.stringify(out));\n"
    ) % json.dumps(cases)

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "helpers.ts"
        f.write_text(stub + body + driver, encoding="utf-8")
        runner = Path(d) / "run.js"
        runner.write_text(
            "const ts=require(%s);const fs=require('fs');\n"
            "const js=ts.transpileModule(fs.readFileSync(%s,'utf8'),"
            "{compilerOptions:{module:ts.ModuleKind.CommonJS,target:'ES2020'}}).outputText;\n"
            "const m={exports:{}};new Function('module','exports','require',js)"
            "(m,m.exports,require);\n"
            % (json.dumps(str(FE / "node_modules/typescript")), json.dumps(str(f))),
            encoding="utf-8",
        )
        r = subprocess.run(["node", str(runner)], capture_output=True, text=True,
                           cwd=str(FE), timeout=120)
    assert r.returncode == 0, "node 跑挂了：\n%s\n%s" % (r.stdout[-2000:], r.stderr[-2000:])
    line = [l for l in r.stdout.splitlines() if l.startswith("@@")]
    assert line, "没拿到结果：\n%s" % r.stdout[-2000:]
    return json.loads(line[-1][2:])


@pytest.mark.parametrize("ui,output,current,expect_follow", [
    # 🚨 客户报的那个 case：中文界面、字幕没手动设过、切成英文出片
    ("zh-CN", "en", None,          "en"),
    ("zh-CN", "en", "zh-Hans",     "en"),
    ("zh-Hant", "en", "zh-Hant",   "en"),
    # 反方向：英文界面用英文字幕，切回中文出片 → 给界面对应的中文
    ("zh-CN", "zh", "en",          "zh-Hans"),
    ("zh-Hant", "zh", "en",        "zh-Hant"),
    # 已经对得上 → 不动（返回 null）
    ("zh-CN", "en", "en",          None),
    ("zh-CN", "zh", "zh-Hans",     None),
    ("en", "en", None,             None),
    # 双语是客户特意挑的 → 两个方向都不动
    ("zh-CN", "en", "zh-Hans+en",  None),
    ("zh-CN", "zh", "zh-Hans+en",  None),
    ("zh-CN", "en", "zh-Hant+en",  None),
])
def test_切换出片语言时字幕怎么跟(ui, output, current, expect_follow):
    got = _run_helper(ui, [{"output": output, "current": current}])[0]
    assert got["follow"] == expect_follow, (
        "界面=%s 出片=%s 当前字幕=%s → 期望 %s，实际 %s"
        % (ui, output, current, expect_follow, got["follow"]))


@pytest.mark.parametrize("ui,output,current,expect", [
    # 没存过 → 按出片语言推。**这是给「存过频道默认值、不会触发 onChange」的客户兜底的**
    ("zh-CN", "en", None, "en"),
    ("zh-CN", "zh", None, "zh-Hans"),
    ("zh-Hant", "zh", None, "zh-Hant"),
    ("en", "en", None, "en"),
    # 客户自己存过 → 一律听客户的，别拿默认值盖掉
    ("zh-CN", "en", "zh-Hans+en", "zh-Hans+en"),
    ("zh-CN", "en", "zh-Hant", "zh-Hant"),
])
def test_没手动选时的兜底看出片语言(ui, output, current, expect):
    got = _run_helper(ui, [{"output": output, "current": current}])[0]
    assert got["effective"] == expect, (
        "界面=%s 出片=%s 存过=%s → 期望 %s，实际 %s"
        % (ui, output, current, expect, got["effective"]))


# ────────────────────────── 四个入口都得接上 ──────────────────────────

def test_工作台短片和长片都联动():
    src = WORKSPACE.read_text(encoding="utf-8")
    assert src.count("subtitleLanguageForOutput") >= 2, \
        "短片/长片两个 onLanguageChange 至少要各接一次"
    # ⚠️ 三个分支都要带上 subPatch：配音本来就是英文的那条走 else，字幕照样可能是错的
    assert src.count("...subPatch") >= 6, \
        "onLanguageChange 有三个分支 × 两处（短/长）= 6 次，漏一个分支就漏一类客户"


def test_工作台提交时的兜底看出片语言():
    src = WORKSPACE.read_text(encoding="utf-8")
    assert "config.subtitleLanguage || subtitleLanguageFromUi()" not in src, \
        "还有地方在按界面语言兜底 —— 存过默认值的客户会拿到中文字幕"
    assert "effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage)" in src


def test_频道批量两处都接上():
    src = CHANNELS.read_text(encoding="utf-8")
    assert "subtitleLanguageForOutput" in src, "切出片语言时没联动字幕"
    assert "defaultBatchSubtitleLanguage(savedBatchDefaults, defaultBatchOutputLanguage" in src
    assert "defaultBatchSubtitleLanguage(defaults, defaultBatchOutputLanguage" in src
    assert "defaultBatchSubtitleLanguage(savedBatchDefaults, subtitleLanguageFromUi())" not in src


def test_旧契约的说明已经改掉():
    """注释里还写着「和出片语言无关」的话，下一个 session 会把这次的修复当 bug 改回去。"""
    src = I18N.read_text(encoding="utf-8")
    assert "Independent of the voiceover/output language" not in src
    assert "effectiveSubtitleLanguage" in src
