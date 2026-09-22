"""TTS 年份发音归一化 —— "1749年" 逐字读成 "一七四九年"。

qwen TTS 对「4 位数 + 年」的读法不稳定:有时读对(一七四九),有时把 1749 当基数
拆成「十七四十九」。间歇性、摸不出规律。**送 TTS 前**把明确的年份转成逐字中文数字,
读法就变确定。与 % 归一化(tts_service.py)同一套模式:正向(送 TTS 前)+ 反向
(字幕显示还原数字),两向都只动发音/显示文本,不碰脚本来源与时间轴。
年份 4 位数 → 4 个中文字,**字数不变**,字幕时间轴零漂移(比 % 规则更安全)。

  · 「公元/公元前」+ 数字 → 逐字(公元明确标年份);
  · 4 位数 + 年 → 逐字,但**排除**整千(X000,多为时长)和时长提示词前缀(长达/历时/
    持续/近/约/了…)。宁可漏转个别年份,也不把时长读错(稳妥 = 少误伤)。
"""
from __future__ import annotations

import re

_D2C = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
        "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
_CN_NUM = "零〇一二三四五六七八九"
_C2D = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
        "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}

# 时长提示词:出现在数字前 → 这是"多少年"的时长,不是年份,不逐字读(稳妥:遇疑不转)。
_DURATION_CUES = ("长达", "历时", "持续", "延续", "存在", "整整", "将近", "历经",
                  "维持", "前后", "足足", "超过", "近", "约", "了", "达")

_RANGE_RE = re.compile(r"(\d{4})(\s*[-—~－至到]\s*)(\d{4})(\s*年)")
_GONGYUAN_RE = re.compile(r"(公元前?)\s*(\d{2,4})(\s*年)?")
_BARE_RE = re.compile(r"(\d{4})(\s*年)")
# 反向:2-4 个连续单字数字 + 年(或年份区间前段);不碰基数词(三千/十八/第一)。
_RESTORE_RE = re.compile(
    rf"[{_CN_NUM}]{{2,4}}(?=年|\s*[-—~－至到]\s*[{_CN_NUM}]{{2,4}}\s*年)")


def _d2c(num: str) -> str:
    return "".join(_D2C.get(ch, ch) for ch in num)


def _has_duration_cue(text: str, at: int) -> bool:
    """数字前 5 字内有没有时长提示词。(前面各步都是等长替换,索引不漂移。)"""
    return any(cue in text[max(0, at - 5):at] for cue in _DURATION_CUES)


def normalize_years_for_tts(text: str) -> str:
    """把明确的年份数字转成逐字中文数字,供 TTS 正确读出。只改发音文本。"""
    s = str(text or "")

    def _range(m: "re.Match[str]") -> str:
        if _has_duration_cue(s, m.start()):
            return m.group(0)
        return _d2c(m.group(1)) + m.group(2) + _d2c(m.group(3)) + m.group(4)
    s = _RANGE_RE.sub(_range, s)

    # 公元/公元前 明确标年份 → 2-4 位逐字(整千如"公元2000年"留原样,TTS 自然读"两千年")。
    def _gongyuan(m: "re.Match[str]") -> str:
        num = m.group(2)
        if num.endswith("000"):
            return m.group(0)
        return m.group(1) + _d2c(num) + (m.group(3) or "")
    s = _GONGYUAN_RE.sub(_gongyuan, s)

    def _bare(m: "re.Match[str]") -> str:
        num = m.group(1)
        if num.endswith("000") or _has_duration_cue(s, m.start()):
            return m.group(0)
        return _d2c(num) + m.group(2)
    return _BARE_RE.sub(_bare, s)


def restore_years_for_display(text: str) -> str:
    """字幕显示层:逐字中文数字年份还原回阿拉伯数字(一七四九年 → 1749年)。

    只匹配 2-4 个**连续单字数字** + 年(或年份区间),绝不碰基数词(三千/十八/第一)
    —— 那些不含在 [零〇一二…九] 的连续串里(十/百/千被排除)。best-effort。
    """
    s = str(text or "")
    return _RESTORE_RE.sub(lambda m: "".join(_C2D[c] for c in m.group(0)), s)
