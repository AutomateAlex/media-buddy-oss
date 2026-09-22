# -*- coding: utf-8 -*-
"""

## 为什么补这一条通道


中文频道走千问、英文走 Google，而客户绝大多数是中文。

> 📌 教训：动手优化之前**先量哪条路用得多**。
>    昨天"一次只动一条通道"的原则没错，错在挑通道时看的是
>    恰好翻到的英文样本，没去数。

## 🚨 千问比 Google 多一层，不能照抄

Google 那条是「调 API → 转码 → 测时长」。
千问多一层**段完整性重试**(`_SEGMENT_MAX_RETRIES`)：

    千问偶尔会吞掉一段文本的尾部，却照样返回 finish_reason="stop"

那一层必须原样保留 —— 丢了它，客户会拿到**少念半句话**的片子，
而且不报错。

## 这组守什么

1. 乱序返回时拼接顺序仍按索引（和 Google 那条同一个坑）
2. 段完整性重试没有在并行改造中丢失
3. 时间码累加发生在排序之后
4. 并行 worker 不碰跨段共享状态
"""

from __future__ import annotations

import ast
import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
TTS = REPO / "src" / "backend" / "services" / "tts_service.py"


def _src() -> str:
    return io.open(TTS, encoding="utf-8-sig").read()


def _fn(name: str, parent: str | None = None) -> ast.FunctionDef:
    tree = ast.parse(_src())
    roots = [tree]
    if parent:
        roots = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == parent]
        assert roots, "找不到函数 %s" % parent
    for root in roots:
        for n in ast.walk(root):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return n
    raise AssertionError("找不到函数 %s" % name)


def _qwen() -> str:
    return ast.get_source_segment(_src(), _fn("_synthesize_qwen")) or ""


def test_千问必须并行():
    """🚨 95% 的出片走这条路。"""
    src = _qwen()
    assert "ThreadPoolExecutor" in src or "_Pool" in src, (
        "千问 TTS 还是串行的 —— 而它承担了 95% 的真实出片")


def test_并行之后必须按索引排序():
    """🚨 和 Google 那条同一个坑：段落乱序 = 音频和字幕全乱，**且不报错**。

    用 AST 判断，不扫源码文本 —— 昨天两次被注释里的字符串骗过。
    """
    node = _fn("_synthesize_qwen")
    ok = False
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if (isinstance(f, ast.Attribute) and f.attr == "sort") or \
           (isinstance(f, ast.Name) and f.id == "sorted"):
            if any(kw.arg == "key" for kw in n.keywords):
                ok = True
    assert ok, "并行结果没有按索引排序 —— 音频会按「谁先返回」拼接"


def test_时间码累加必须在排序之后():
    """⚠️ `start=total_duration` 依赖前面所有段的总时长。"""
    node = _fn("_synthesize_qwen")
    sort_l, acc_l = [], []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == "sort") or \
               (isinstance(f, ast.Name) and f.id == "sorted"):
                sort_l.append(n.lineno)
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) \
                and n.target.id == "total_duration":
            acc_l.append(n.lineno)
    assert sort_l, "没有排序"
    assert acc_l, "找不到 total_duration 累加"
    assert min(acc_l) > min(sort_l), "累加发生在排序之前 —— 字幕时间码会错位"


def test_段完整性重试不许丢():
    """🚨 **千问独有、比顺序更隐蔽的坑。**

    这一层重试是唯一能发现它的机制。并行改造时丢了它 ——
    客户会拿到**少念半句话**的片子，而且没有任何报错。
    """
    src = _qwen()
    assert "_SEGMENT_MAX_RETRIES" in src, "段完整性重试的上限没了"
    assert "_segment_duration_ok" in src, (
        "段时长校验没了 —— 千问吞字将再也无人察觉")
    assert re.search(r"best_dur|best_seg", src), (
        "「取最长的一版」的兜底没了 —— 重试用尽时会整条失败，"
        "而原来的行为是发最长的那版")


def test_并行worker不碰跨段状态():
    """🚨 多线程竞态，结果不可复现。"""
    inner = None
    for name in ("_one_segment", "_synth_one", "_qwen_one"):
        try:
            inner = _fn(name, parent="_synthesize_qwen")
            break
        except AssertionError:
            continue
    assert inner is not None, "找不到并行 worker 函数"
    shared = {"total_duration", "seg_paths", "all_tokens", "total_chars"}
    touched = {n.id for n in ast.walk(inner)
               if isinstance(n, ast.Name) and n.id in shared}
    assert not touched, "并行 worker 碰了跨段共享状态 %s" % sorted(touched)


def test_并发数可配且和Google共用一个开关():
    """📌 两条通道用同一个 `MEDIA_BUDDY_TTS_CONCURRENCY`。

    分开配会让运维在出事时改错地方 —— 尤其是「降回串行止血」的时候。
    """
    src = _qwen()
    assert "MEDIA_BUDDY_TTS_CONCURRENCY" in src, (
        "千问没读并发开关 —— 出事时没法一键降回串行")


def test_鉴权错仍然快停():
    """📌 400/401/403 重试没用，原来的快停行为不能丢。"""
    src = _qwen()
    assert "401" in src and "403" in src, "丢掉了鉴权错的快停"


def test_后面的段先返回时顺序仍然正确(monkeypatch, tmp_path):
    """🚨 **本文件最重要的一条** —— 真跑一遍，强制乱序返回。

    源码守卫只能证明"写了排序"，证明不了"排序真的对"。
    """
    import sys, time as _t, types, json
    sys.path.insert(0, str(REPO / "src"))
    from backend.services.tts_service import TTSService

    CHUNKS = ["甲甲甲", "乙乙乙", "丙丙丙", "丁丁丁", "戊戊戊"]
    svc = TTSService()

    monkeypatch.setattr(TTSService, "_split_tts_gateway_chunks",
                        staticmethod(lambda text, max_chars=480: list(CHUNKS)))
    monkeypatch.setattr(TTSService, "_probe_audio_duration", lambda self, p: 1.0)

    returned: list[str] = []

    class _Resp:
        def __init__(self, p): self._p = p
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        text = body["input"]["text"]
        # 🚨 反着睡：第 1 段最慢，最后一段秒回
        _t.sleep(0.30 - 0.05 * CHUNKS.index(text))
        returned.append(text)
        return _Resp(json.dumps({
            "output": {"finish_reason": "stop",
                       "audio": {"url": "http://fake/%s.wav" % text}},
            "usage": {"characters": len(text)},
        }).encode())

    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", fake_urlopen)

    def fake_retrieve(url, dest):
        # url 里带着段文本 → 写进假 wav，后面 ffmpeg 原样搬运
        name = url.split("/")[-1].replace(".wav", "")
        pathlib.Path(dest).write_bytes(name.encode())
    monkeypatch.setattr(_u, "urlretrieve", fake_retrieve)

    import subprocess as _sp
    def fake_run(cmd, **kw):
        out = pathlib.Path(cmd[-1])
        if "concat" in cmd:
            lst = pathlib.Path(cmd[cmd.index("-i") + 1])
            data = b""
            for line in lst.read_text(encoding="utf-8").splitlines():
                data += pathlib.Path(line.split("'")[1]).read_bytes()
            out.write_bytes(data)
        else:
            src = pathlib.Path(cmd[cmd.index("-i") + 1])
            out.write_bytes(src.read_bytes() if src.exists() else b"")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/ffmpeg")

    import backend.services.tts_service as m
    monkeypatch.setattr(m, "_segment_duration_ok", lambda *a, **k: True)
    monkeypatch.setenv("MEDIA_BUDDY_QWEN_API_KEY", "fake")
    monkeypatch.setenv("MEDIA_BUDDY_TTS_CONCURRENCY", "5")

    out = tmp_path / "narration.mp3"
    res = svc._synthesize_qwen("ignored", str(out), speed=1.0, voice="Cherry")

    assert res.success, "合成失败: %s" % res.error
    assert returned != CHUNKS, (
        "各段是按顺序返回的 —— 这条测试没起到作用，调整 sleep 让它乱序")
    got = out.read_bytes().decode()
    assert got == "".join(CHUNKS), (
        "拼接顺序错了！\n  实际 %s\n  应为 %s\n  (返回顺序 %s)"
        % (got, "".join(CHUNKS), returned))
