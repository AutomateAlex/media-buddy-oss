"""

## 修的是什么

脱敏规则本来就有（`_REDACTIONS`），但**只在读日志生成诊断报告时才用**。

  1. 有些第三方接口只接受把 key 放查询参数（如 Pixabay `?key=...`）
  2. `requests` / `httpx` 抛的异常**文本里带完整 URL**
  3. 代码多处用 `%s` 直接记录异常对象
  4. worker 全局日志 INFO，httpx 自己也打印每个请求 URL

修法：在**出口**统一套一层脱敏 Formatter，而不是逐个改调用点（改不完，新代码还会再犯）。
"""

from __future__ import annotations

import logging

import pytest

from backend.lib.diagnostics import (
    RedactingFormatter,
    install_log_redaction,
    redact,
)

GOOGLE_KEY = "AIzaSyC7xQvExampleKeyValue1234567890abc"
PIXABAY_KEY = "51234567-abcdef0123456789abcdef012"


def _capture(logger: logging.Logger) -> tuple[logging.Handler, list[str]]:
    """装一个把格式化结果收进列表的 handler，模拟"落盘"。"""

    lines: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _Collector()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return handler, lines


@pytest.fixture
def logger():
    log = logging.getLogger("test.redaction")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    yield log
    for handler in list(log.handlers):
        log.removeHandler(handler)


# ── 规则本身 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        f"HTTP Request: GET https://www.googleapis.com/youtube/v3/search?q=x&key={GOOGLE_KEY}",
        f"503 Server Error for url: https://pixabay.com/api/videos/?key={PIXABAY_KEY}&q=cat",
        f"failed with api_key={GOOGLE_KEY}",
    ],
)
def test_keys_in_urls_are_redacted(text):
    out = redact(text)

    assert GOOGLE_KEY not in out
    assert PIXABAY_KEY not in out
    assert "***" in out


def test_bearer_tokens_are_redacted():
    out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef")

    assert "eyJhbGci" not in out
    assert "Bearer ***" in out


# ── 出口拦截（这才是这次修的重点）────────────────────────────


def test_formatted_message_is_redacted_before_it_reaches_the_handler(logger):
    handler, lines = _capture(logger)
    handler.setFormatter(RedactingFormatter(handler.formatter))

    logger.warning("call failed: %s", f"https://api.example.com/x?key={GOOGLE_KEY}")

    assert GOOGLE_KEY not in lines[0]
    assert "***" in lines[0]


def test_redaction_covers_args_not_just_the_format_string(logger):
    """Filter 只能看到未格式化的 record —— 参数里的密钥会漏掉。所以用 Formatter。"""

    handler, lines = _capture(logger)
    handler.setFormatter(RedactingFormatter(handler.formatter))

    logger.warning("%s failed for %s", "pixabay", f"https://pixabay.com/api/?key={PIXABAY_KEY}")

    assert PIXABAY_KEY not in lines[0]


def test_redaction_covers_exception_tracebacks(logger):
    """`requests` 的异常文本里带完整 URL —— traceback 也必须脱敏。"""

    handler, lines = _capture(logger)
    handler.setFormatter(RedactingFormatter(handler.formatter))

    try:
        raise RuntimeError(f"503 for url: https://pixabay.com/api/?key={PIXABAY_KEY}")
    except RuntimeError:
        logger.exception("stock search crashed")

    assert PIXABAY_KEY not in lines[0]


def test_inner_formatter_layout_is_preserved(logger):
    handler, lines = _capture(logger)
    handler.setFormatter(RedactingFormatter(handler.formatter))

    logger.warning("plain message")

    assert lines[0] == "WARNING test.redaction: plain message"


# ── 安装 ──────────────────────────────────────────────────────


def test_installed_redaction_catches_third_party_log_lines(logger):
    """httpx 自己那行 INFO 日志不是我们写的，但它带着完整 URL。

    这正是「在出口拦」相对「逐个改调用点」的价值 —— 第三方库改不了。
    """

    _, lines = _capture(logger)
    install_log_redaction(logger)

    logging.getLogger("test.redaction").info(
        'HTTP Request: GET https://www.googleapis.com/youtube/v3/search?key=%s "HTTP/1.1 200 OK"',
        GOOGLE_KEY,
    )

    # ⚠️ 断言的是**不变量**（密钥没了），不是具体占位符文字。
    # 密钥同样没了，还多告诉你「这是个 Google key」，对排查更有用。
    # 把测试写死成某个占位符，会让每次加强脱敏都误报成回归。
    assert GOOGLE_KEY not in lines[0], "密钥明文还在日志里"
    assert "***" in lines[0], "应该留下脱敏痕迹，而不是整段消失"


#
# 里面是完整的 YouTube key。当时的脱敏规则只认 `key=xxx` 这种**带前缀**的形状，
# 裸露的密钥、以及 TOKEN=/SECRET= 这些整行 env，一律漏网。


def test_bare_google_key_is_redacted_without_a_key_prefix():
    """⚠️ 真实的泄漏形状常常没有 `key=` 前缀。

    Google 自己的报错就是这样：`API key AIzaSy... is invalid`。
    只认 `key=` 的规则对它完全无效。
    """

    out = redact(f"API key {GOOGLE_KEY} is invalid or has expired")

    assert GOOGLE_KEY not in out
    assert "AIza***" in out


def test_env_line_pasted_into_a_log_is_redacted():
    """整行 env 被贴进日志/异常里 —— 这次真实发生过的形状。"""

    out = redact(f"MB_YOUTUBE_API_KEY={GOOGLE_KEY}")

    assert GOOGLE_KEY not in out


def test_token_and_secret_env_shapes_are_redacted():
    """`key=` 那条规则盖不住 TOKEN / SECRET / PASSWORD。"""

    for line in (
        "OPENROUTER_TOKEN=or-abcdefghijklmnopqrstuvwxyz",
        "SUPABASE_SERVICE_SECRET=svc_abcdefghijklmnop1234",
        "DB_PASSWORD=hunter2hunter2hunter2",
    ):
        out = redact(line)
        assert out.endswith("***"), f"没脱敏：{out}"


def test_database_url_password_is_redacted():
    """运维脚本到处打连接串，一打就是明文密码。"""

    out = redact(
        "connecting to postgresql+psycopg2://mbuser:S3cretPa55@db.example.com:5432/postgres"
    )

    assert "S3cretPa55" not in out
    assert "mbuser" in out, "用户名和主机要留着，否则排查时看不出连的是哪个库"


def test_jwt_is_redacted():
    out = redact(
        "session=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"
    )

    assert "eyJzdWIiOiIxMjM0NTY3ODkwIn0" not in out


def test_other_vendor_key_shapes():
    for secret, marker in (
        ("sk-ant-api03-" + "a" * 40, "sk-ant-***"),
        ("sk-proj-" + "b" * 40, "sk-proj-***"),
        ("sk-" + "c" * 40, "sk-***"),
        ("ghp_" + "d" * 36, "ghp_***"),
        ("AKIAIOSFODNN7EXAMPLE", "AKIA***"),
    ):
        out = redact(f"credential is {secret} ok")
        assert secret not in out, f"没脱敏：{secret[:10]}..."
        assert marker in out


def test_redaction_does_not_eat_normal_text():
    """脱敏不能把正常日志毁掉 —— 过度脱敏会让日志失去排查价值。"""

    normal = "radar yt: 搜索 'houdini vfx' 取到 12 条，花 102 units（剩余 7257）"

    assert redact(normal) == normal
