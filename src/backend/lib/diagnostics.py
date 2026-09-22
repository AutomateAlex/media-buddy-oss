"""Local diagnostics helpers for supportable desktop builds."""
from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".media-buddy-oss" / "logs"
BACKEND_LOG = LOG_DIR / "backend.log"
PIPELINE_DIR = Path.home() / ".media-buddy-oss" / "pipelines"

_CONFIGURED = False

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # ── 按「密钥自己的形状」认，不依赖前面有没有 key= ──────────────
    # 裸露的密钥照样明文落盘。真实的泄漏形状不止一种：
    #   · Google 报错：`API key AIzaSy... is invalid`（没有 key= 前缀）
    #   · JSON 响应体、配置片段被 %s 打进日志
    #   · 有人(包括 AI 助手)在排查时把 env 文件整行打出来
    # 按形状认才关得住 —— 每一条都以厂商的固定前缀开头，误伤正常文本的概率极低。
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "AIza***"),            # Google / YouTube
    (re.compile(r"sk-or-v1-[A-Za-z0-9._-]+"), "sk-or-v1-***"),      # OpenRouter
    (re.compile(r"sk-ant-[A-Za-z0-9._-]{20,}"), "sk-ant-***"),      # Anthropic
    (re.compile(r"sk-proj-[A-Za-z0-9._-]{20,}"), "sk-proj-***"),    # OpenAI 项目密钥
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "sk-***"),               # OpenAI / 阿里百炼 兜底
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
     "eyJ***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "ghp_***"),             # GitHub
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Bearer ***"),
    (re.compile(r"fal_[A-Za-z0-9:_-]{16,}"), "fal_***"),
    # ── 带前缀的兜底（形状不认识的厂商靠这两条）──────────────────
    (re.compile(r"(api[_-]?key=)[A-Za-z0-9._:-]{16,}", re.I), r"\1***"),
    (re.compile(r"(key=)[A-Za-z0-9._:-]{16,}", re.I), r"\1***"),
    # `NAME_KEY=值` / `NAME_TOKEN=值` / `NAME_SECRET=值` 这种整行 env 形状。
    # 上面那条 `key=` 盖不住 TOKEN 和 SECRET,而排查时最容易被整行打出来的就是它们。
    (re.compile(r"([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD)\s*=\s*)\S{8,}"), r"\1***"),
    # 数据库连接串里的密码。运维脚本里到处是它,一打就是明文。
    (re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"), r"\1***\2"),
    (re.compile(r"https://media-buddy\.com"), "Media Buddy Cloud"),
    (re.compile(r"/api/v1/[A-Za-z0-9_./-]+"), "[cloud endpoint]"),
    (re.compile(r"C:\\Users\\[^\\\s]+\\\.media-buddy-oss", re.I), "%USERPROFILE%\\.media-buddy-oss"),
    (re.compile(r"\bQStash\b", re.I), "worker"),
    (re.compile(r"\bfal\.ai\b", re.I), "AI video provider"),
    (re.compile(r"\bOpenRouter\b", re.I), "AI text provider"),
    (re.compile(r"\bmaster key\b", re.I), "provider credential"),
    (re.compile(r"\baiGateway tier\b", re.I), "cloud rate limit"),
    (re.compile(r"\bPexels gateway yet \(Phase 3\.2\)\b", re.I), "stock media gateway"),
]


def redact(text: str) -> str:
    out = str(text or "")
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFormatter(logging.Formatter):
    """

    **读日志生成诊断报告**时才用（`tail_log`）。写日志时不脱敏 ——

      1. 有些第三方接口只接受把 key 放查询参数（如 Pixabay `?key=...`）
      2. `requests` / `httpx` 抛的异常**文本里带完整 URL**
      3. 代码多处用 `%s` 直接记录异常对象（如 `footage_service.py:427,665`）
      4. worker 全局日志级别是 INFO，httpx 自己也会打印每个请求 URL

    **为什么用 Formatter 而不是 Filter**：Filter 拿到的是未格式化的 record，
    看不到 `args` 展开后的结果，也看不到异常堆栈。Formatter 拿到的是最终文本，
    一次覆盖消息、参数、traceback 三者。

    **为什么不逐个改调用点**：改不完，而且新代码还会再犯。
    在出口统一拦一道，是唯一能真正关闭这一类问题的做法。
    """

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))


def install_log_redaction(logger: logging.Logger | None = None) -> int:
    """给指定 logger（默认 root）上的每个 handler 套一层脱敏。幂等，返回新套上的个数。

    三个入口都要调：
      - `configure_file_logging()` —— API / 桌面端（写文件）
      - `workers/studio_long_worker.py::main()` —— 同上

    worker 那两个尤其重要：**它们不写日志文件，只打 stdout**，
    """

    root = logger or logging.getLogger()
    wrapped = 0
    for handler in root.handlers:
        if isinstance(handler.formatter, RedactingFormatter):
            continue
        handler.setFormatter(RedactingFormatter(handler.formatter))
        wrapped += 1
    return wrapped


def configure_file_logging() -> Path:
    """Attach a rotating backend log file once per process."""
    global _CONFIGURED
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "backend file logging disabled; cannot create %s: %s", LOG_DIR, e
        )
        return BACKEND_LOG
    if _CONFIGURED:
        return BACKEND_LOG

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            try:
                if Path(handler.baseFilename) == BACKEND_LOG:
                    _CONFIGURED = True
                    return BACKEND_LOG
            except Exception:
                pass

    try:
        handler = RotatingFileHandler(
            BACKEND_LOG,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except Exception as e:
        logging.getLogger(__name__).warning(
            "backend file logging disabled; cannot open %s: %s", BACKEND_LOG, e
        )
        return BACKEND_LOG
    handler.setFormatter(RedactingFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )))
    root.addHandler(handler)
    # 顺手把已有的 handler(如 basicConfig 装的 StreamHandler)也套上 ——
    # 光管自己这个文件 handler，stdout 那条路照样明文外泄。
    install_log_redaction(root)
    _CONFIGURED = True
    logging.getLogger(__name__).info("backend file logging enabled: %s", BACKEND_LOG)
    return BACKEND_LOG


def tail_log(lines: int = 300) -> list[str]:
    limit = max(20, min(int(lines or 300), 1000))
    if not BACKEND_LOG.exists():
        return []
    with BACKEND_LOG.open("r", encoding="utf-8", errors="replace") as f:
        return [redact(line.rstrip("\n")) for line in deque(f, maxlen=limit)]


def read_progress_errors(project_id: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    project_dir = PIPELINE_DIR / project_id
    for stage in ("script", "assets", "music", "compose"):
        p = project_dir / f"{stage}.progress.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errors[stage] = f"progress read failed: {e}"
            continue
        err = data.get("error") or (data.get("artifacts") or {}).get("error")
        if err:
            errors[stage] = redact(str(err))
    return errors


def public_path(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if p.name:
        return p.name
    return redact(str(path))


def public_error(error: str) -> str:
    msg = redact(error)
    if "All chunks failed" in msg or "0 stock matches" in msg:
        return (
            "素材获取失败：没有找到可用素材，云端生成也没有返回结果。"
            "请刷新后重试，或把诊断包发给支持。"
        )
    if "No valid footage clips" in msg:
        return "合成失败：没有可用视频片段。请重试或发送诊断包。"
    return msg


def cloud_snapshot() -> dict[str, Any]:
    try:
        from backend.lib.cloud_auth import get_default_auth
        authenticated = bool(get_default_auth().is_authenticated)
    except Exception:
        authenticated = False
    return {
        "base_url": "local",
        "desktop_authenticated": authenticated,
    }
