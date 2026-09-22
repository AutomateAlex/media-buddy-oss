"""Phase 2.10a — Agent safety library (L4 / L5 / L6 defenses).

L1 (system prompt hard-bound) and L2 (tool whitelist) live in StudioAgent.
L3 (input validation) lives inside each tool's wrapper.

Here we centralize:
  L4  Output filtering — scrub LLM responses for leaked keys / system prompt
  L5  Rate limiting — max turns/session and max messages/minute
  L6  Prompt injection detection — pattern match common jailbreak attempts

The standard refusal template is also defined here so all layers respond
identically when triggered.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

REFUSAL_TEMPLATE = (
    "抱歉，我只能帮你制作视频。我们换个话题——你想做什么样的视频？"
)


# ----------------------------------------------------------------------
# L6 — Prompt injection detection (patterns the LLM should never obey)
# ----------------------------------------------------------------------

# Case-insensitive substring patterns. Hits → return REFUSAL_TEMPLATE without
# ever forwarding to the LLM. Conservative — false positives are fine
# (the user can rephrase) since this only fires on obvious abuse.
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(prior|previous|above)\s+instructions?",
    r"disregard\s+(all\s+)?(prior|previous|above)\s+instructions?",
    r"忽略.{0,8}(之前|前面|以上).{0,8}(指令|规则|限制)",
    r"forget\s+(everything|all)\s+(before|above|prior)",
    r"system\s*prompt",
    r"你的\s*(system|系统)\s*(prompt|提示)",
    r"reveal\s+your\s+(prompt|instructions|system)",
    r"you\s+are\s+now\s+",
    r"you\s+will\s+now\s+",
    r"假装你是",
    r"pretend\s+(to\s+be|you\s+are)",
    r"\bact\s+as\s+(?!a\s+video)",  # "act as a hacker" but allow "act as a video..."
    r"jailbreak",
    r"DAN\s+mode",
]

_INJECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def detect_prompt_injection(text: str) -> Optional[str]:
    """Return the matched pattern name if user input looks like a jailbreak.
    None = clean."""
    if not text:
        return None
    for rx in _INJECTION_REGEX:
        m = rx.search(text)
        if m:
            return m.group(0)[:80]
    return None


# ----------------------------------------------------------------------
# L4 — Output filtering (catch LLM occasionally leaking)
# ----------------------------------------------------------------------

# Patterns that should NEVER appear in agent output. If any match,
# replace the whole reply with REFUSAL_TEMPLATE.
_FORBIDDEN_OUTPUT_PATTERNS = [
    r"OPENROUTER_API_KEY",
    r"DASHSCOPE_API_KEY",
    r"ANTHROPIC_API_KEY",
    r"OPENAI_API_KEY",
    r"PEXELS_API_KEY",
    r"PIXABAY_API_KEY",
    r"FAL_KEY",
    r"FREESOUND_API_KEY",
    r"sk-[A-Za-z0-9_-]{20,}",      # generic OpenAI-style key (incl. sk-proj-*, sk-svcacct-*)
    r"sk-ant-[A-Za-z0-9_-]{20,}",  # Anthropic key
    # Don't reveal the agent's own system prompt
    r"You\s+are\s+MediaBuddy'?s?\s+video\s+production\s+assistant",
    r"你是\s*MediaBuddy\s*的视频制作助手",
]
_FORBIDDEN_OUTPUT_REGEX = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_OUTPUT_PATTERNS]


def scrub_output(text: str) -> tuple[str, Optional[str]]:
    """Returns (safe_text, hit_pattern_or_None). If hit, safe_text is
    REFUSAL_TEMPLATE; otherwise text is unchanged."""
    if not text:
        return text, None
    for rx in _FORBIDDEN_OUTPUT_REGEX:
        m = rx.search(text)
        if m:
            return REFUSAL_TEMPLATE, m.group(0)[:80]
    return text, None


# ----------------------------------------------------------------------
# L5 — Rate limiting (per-session)
# ----------------------------------------------------------------------

# Per-session caps: 50 turns lifetime + 20 messages per rolling 60s window.
MAX_TURNS_PER_SESSION = 50
MAX_MSG_PER_MINUTE = 20

_msg_log: dict[str, deque[float]] = defaultdict(deque)
_msg_log_lock = Lock()


def check_rate_limit(session_id: str) -> Optional[str]:
    """Returns a human-readable reason if rate-limited, else None."""
    now = time.time()
    with _msg_log_lock:
        log = _msg_log[session_id]
        # Trim old entries
        while log and now - log[0] > 60.0:
            log.popleft()
        if len(log) >= MAX_MSG_PER_MINUTE:
            return f"消息频率过高（每分钟最多 {MAX_MSG_PER_MINUTE} 条）"
        log.append(now)
    return None


def reset_rate_limit_for_session(session_id: str) -> None:
    """Clear rate-limit state — called on session close. Optional cleanup."""
    with _msg_log_lock:
        _msg_log.pop(session_id, None)
