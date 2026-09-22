#!/usr/bin/env python3
"""独立强制对齐脚本 —— 用 faster-whisper 把【已合成音频】对齐到【已知文稿】,
产出每个字的真实毫秒时间码(替换千问那套按字数均摊的估算)。

为什么独立成脚本 + 独立 venv:faster-whisper(ctranslate2/onnx)依赖较重,
不能塞进主渲染 venv(怕依赖冲突)。渲染管线以【子进程】方式调它:
    <aligner_python> forced_align.py <audio_path> <text_file>
成功 → stdout 打印 JSON:{"ok": true, "tokens": [{"kind":"char","text":..,"start":..,"end":..}, ...]}
失败/无 faster-whisper → 打印 {"ok": false, ...},调用方退回估算,绝不因此挂出片。

用 whisper 的时间(真实)+ 我们的文稿文字(准确,无同音字错):difflib 对齐两条字符序列,
匹配上的字直接用 whisper 真时间,没匹配上的(whisper 同音字/漏字处)用相邻真值线性插值。

env:
  MEDIA_BUDDY_ALIGN_MODEL   faster-whisper 模型(默认 small)
  MEDIA_BUDDY_ALIGN_CACHE   模型缓存目录(避免每次下载)
  MEDIA_BUDDY_ALIGN_DEVICE  cpu(默认)/ cuda
"""
import json
import os
import re
import sys
import difflib


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    if len(sys.argv) < 3:
        _emit({"ok": False, "error": "usage: forced_align.py <audio> <text_file>"})
        return 0
    audio_path = sys.argv[1]
    text_path = sys.argv[2]
    try:
        with open(text_path, encoding="utf-8") as f:
            script_text = f.read()
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": "read text failed: %s" % e})
        return 0

    # 文稿字序列(去空白,与 char token 口径一致)
    sc = [c for c in script_text if not c.isspace()]
    if not sc:
        _emit({"ok": False, "error": "empty script"})
        return 0

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # noqa: BLE001 — 没装就让调用方退回估算
        _emit({"ok": False, "error": "faster_whisper import failed: %s" % e})
        return 0

    model_name = os.environ.get("MEDIA_BUDDY_ALIGN_MODEL", "small").strip() or "small"
    cache_dir = os.environ.get("MEDIA_BUDDY_ALIGN_CACHE", "").strip() or None
    device = os.environ.get("MEDIA_BUDDY_ALIGN_DEVICE", "cpu").strip() or "cpu"
    compute = "int8" if device == "cpu" else "float16"

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute, download_root=cache_dir)
        #
        # 原来是 `language="zh"`。英文旁白被强制按中文转写 → 出来一堆音译汉字 →
        # 和英文稿子逐字比对 → **match_rate 0.00~0.06** → 字幕时间全靠插值猜 →
        # 客户看到的就是「字幕和音频对不齐」。
        #
        # 那不是「差一点」,是**完全对不上**。
        #
        # 现在按调用方给的语言来;没给就让 whisper 自己判(None)。
        # ⚠️ 中文能给就给明确值 —— 短音频上自动判语言偶尔会判错。
        lang = os.environ.get("MEDIA_BUDDY_ALIGN_LANG", "").strip().lower() or None
        if lang in ("", "auto", "none"):
            lang = None
        segments, _info = model.transcribe(
            audio_path, language=lang, word_timestamps=True)
        # whisper 字级时间(多字词按字均分)
        wchars = []  # (char, start, end)
        for s in segments:
            for w in (s.words or []):
                txt = re.sub(r"\s", "", w.word or "")
                n = len(txt)
                if n == 0:
                    continue
                step = (float(w.end) - float(w.start)) / n
                for i, c in enumerate(txt):
                    wchars.append((c, float(w.start) + i * step, float(w.start) + (i + 1) * step))
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": "transcribe failed: %s" % e})
        return 0

    if not wchars:
        _emit({"ok": False, "error": "no whisper words"})
        return 0

    # 对齐:whisper 字序列 vs 文稿字序列。匹配上的用真时间,其余相邻插值。
    wc = [c for c, _, _ in wchars]
    sm = difflib.SequenceMatcher(None, wc, sc, autojunk=False)
    times = [None] * len(sc)
    matched = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[j1 + k] = (wchars[i1 + k][1], wchars[i1 + k][2])
                matched += 1

    total_dur = wchars[-1][2]
    # 未匹配字:用左邻真结束时间 → 下一个有时间的字起点之间线性铺开
    last = 0.0
    for j in range(len(sc)):
        if times[j] is not None:
            last = times[j][1]
            continue
        nxt = None
        for k in range(j + 1, len(sc)):
            if times[k] is not None:
                nxt = times[k][0]
                break
        if nxt is None:
            nxt = total_dur
        # 该 gap 内均分给连续未匹配字
        run_end = j
        while run_end + 1 < len(sc) and times[run_end + 1] is None:
            run_end += 1
        cnt = run_end - j + 1
        span = max(0.01, (nxt - last) / cnt)
        for t in range(cnt):
            s0 = last + t * span
            times[j + t] = (round(s0, 3), round(s0 + span, 3))
        last = times[run_end][1]

    # 单调性兜底 + 输出
    tokens = []
    prev_end = 0.0
    for c, (s0, e0) in zip(sc, times):
        s0 = max(prev_end, float(s0))
        e0 = max(s0 + 0.01, float(e0))
        tokens.append({"kind": "char", "text": c, "start": round(s0, 3), "end": round(e0, 3)})
        prev_end = e0

    _emit({"ok": True, "tokens": tokens,
           "matched": matched, "total": len(sc),
           "match_rate": round(matched / max(1, len(sc)), 3)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
