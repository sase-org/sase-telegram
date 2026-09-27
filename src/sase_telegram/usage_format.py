"""Pure HTML rendering for the Telegram /usage command."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone, UTC
from typing import Any

from sase_telegram.agent_format import html_escape, pack_html_blocks

__all__ = [
    "build_usage_blocks",
    "build_usage_chunks",
    "format_attention_dot",
    "format_capacity_bar",
    "format_provider_header",
    "format_reset_text",
    "format_short_percent",
    "format_window_label",
    "render_headline",
]


def format_capacity_bar(remaining_percent: float, *, empty: bool = False) -> str:
    """Return a 10-cell capacity bar using TUI Usage meter glyphs."""
    if empty:
        return "░" * 10
    try:
        remaining = float(remaining_percent)
    except (TypeError, ValueError):
        remaining = 0.0
    if not math.isfinite(remaining):
        remaining = 0.0
    clamped = max(0.0, min(100.0, remaining))
    filled = round(clamped / 10.0)
    filled = max(0, min(10, int(filled)))
    if clamped >= 1.0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (10 - filled)


def format_short_percent(remaining_text: str) -> str:
    """Strip the core trailing ' left' from remaining text."""
    text = str(remaining_text or "")
    if text.endswith(" left"):
        text = text[: -len(" left")]
    return text or "0%"


def _is_exhausted(window: Any) -> bool:
    attention = str(getattr(window, "attention", "") or "")
    if attention == "rejected":
        return True
    exceeded = getattr(window, "exceeded_by_percent", None)
    if isinstance(exceeded, bool):
        return False
    if isinstance(exceeded, (int, float)) and math.isfinite(float(exceeded)):
        return float(exceeded) > 0
    return False


def format_attention_dot(window: Any, *, provider_problem: bool = False) -> str:
    """Return the status dot for one window."""
    if _is_exhausted(window):
        return "⛔"
    attention = str(getattr(window, "attention", "") or "")
    if attention == "very_low":
        return "🔴"
    if attention == "low":
        return "🟡"
    freshness = str(getattr(window, "freshness", "") or "")
    reset_state = str(getattr(window, "reset_state", "") or "")
    if freshness != "fresh" or reset_state == "passed" or provider_problem:
        return "⚪"
    return "🟢"


def _title_case_token(token: str) -> str:
    if re.fullmatch(r"[a-z0-9]{1,3}", token):
        return token
    return token[:1].upper() + token[1:].lower() if token else token


def _compact_duration_token(seconds: float | None) -> str | None:
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    total_minutes = round(seconds / 60.0)
    if total_minutes <= 0:
        return None
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days and not hours and not minutes:
        return f"{days}d"
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours and not minutes:
        return f"{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def format_window_label(window: Any) -> str:
    """Return the '{scope} · {period}' label for one window."""
    vendor_fallback = str(
        getattr(window, "label", "") or getattr(window, "key", "") or ""
    )
    period = str(getattr(window, "period", "") or "unknown")
    scope = str(getattr(window, "scope", "") or "unknown")
    if period == "session":
        period_text = "5h"
    elif period == "weekly":
        period_text = "Weekly"
    elif period == "monthly":
        period_text = "Monthly"
    elif period == "duration":
        period_text = (
            _compact_duration_token(getattr(window, "duration_seconds", None))
            or vendor_fallback
        )
    else:
        period_text = vendor_fallback or "?"
    if scope in {"all_models", "product"}:
        label = period_text
    elif scope == "models":
        models = tuple(getattr(window, "scope_models", ()) or ())
        parts = [_title_case_token(str(item)) for item in models if str(item)]
        scope_text = "+".join(dict.fromkeys(parts)) or vendor_fallback
        label = f"{scope_text} · {period_text}"
    elif scope == "model_family":
        family = str(getattr(window, "scope_family", "") or "")
        if family:
            scope_text = _title_case_token(family)
        else:
            models = tuple(getattr(window, "scope_models", ()) or ())
            parts = [_title_case_token(str(item)) for item in models if str(item)]
            scope_text = "+".join(dict.fromkeys(parts)) or vendor_fallback
        label = f"{scope_text} · {period_text}"
    else:
        scope_text = (
            str(getattr(window, "scope_vendor_label", "") or "") or vendor_fallback
        )
        label = f"{scope_text} · {period_text}"
    if len(label) > 18:
        label = label[:17] + "…"
    return label


def _format_age(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "unknown"
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        remainder = minutes % 60
        return f"{hours}h" if remainder == 0 else f"{hours}h {remainder}m"
    days = hours // 24
    return f"{days}d"


def _format_countdown_two_unit(seconds: float) -> str:
    if not math.isfinite(seconds):
        seconds = 0.0
    total = max(float(seconds), 0.0)
    if total < 60.0:
        return "1m"
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def format_reset_text(window: Any, *, now: float, tzinfo: Any) -> str | None:
    """Return the reset suffix for one window, without the '↻' prefix."""
    resets_at = getattr(window, "resets_at", None)
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    if not math.isfinite(float(resets_at)):
        return None
    reset_state = str(getattr(window, "reset_state", "") or "unknown")
    if reset_state == "passed" or float(resets_at) <= now:
        return "reset, refreshing…"
    seconds_until = getattr(window, "seconds_until_reset", None)
    if isinstance(seconds_until, bool) or not isinstance(seconds_until, (int, float)):
        seconds_until = float(resets_at) - now
    if not math.isfinite(float(seconds_until)):
        seconds_until = float(resets_at) - now
    if float(seconds_until) < 86400:
        return f"in {_format_countdown_two_unit(float(seconds_until))}"
    try:
        tz = tzinfo if tzinfo is not None else UTC
        moment = datetime.fromtimestamp(float(resets_at), tz=tz)
        return moment.strftime("%a %H:%M")
    except Exception:
        return None


def _window_sort_key(window: Any) -> tuple:
    return (str(getattr(window, "key", "") or ""),)


def format_provider_header(provider: Any, *, now: float) -> str:
    """Return the provider header line."""
    emoji = getattr(provider, "emoji", None) or "•"
    display_name = str(
        getattr(provider, "display_name", "") or getattr(provider, "provider", "") or ""
    )
    parts = f"{emoji} <b>{html_escape(display_name)}</b>"
    plan = getattr(provider, "plan", None)
    if isinstance(plan, str) and plan.strip():
        parts += f" · <i>{html_escape(plan.strip())}</i>"
    last_observed = getattr(provider, "last_observed_at", None)
    age: float | None = None
    if isinstance(last_observed, (int, float)) and not isinstance(last_observed, bool):
        if math.isfinite(float(last_observed)):
            age = max(float(now) - float(last_observed), 0.0)
    if age is None:
        ages = []
        for window in getattr(provider, "windows", ()) or ():
            value = getattr(window, "age_seconds", None)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ):
                ages.append(float(value))
        if ages:
            age = min(ages)
    age_text = _format_age(age)
    windows = tuple(getattr(provider, "windows", ()) or ())
    stale = any(
        str(getattr(item, "freshness", "") or "") != "fresh" for item in windows
    )
    if stale:
        parts += f" · ⚠️ <i>stale {html_escape(age_text)}</i>"
    else:
        parts += f" · <i>{html_escape(age_text)} ago</i>"
    return parts


def _headline_level(window: Any) -> int:
    if _is_exhausted(window):
        return 3
    attention = str(getattr(window, "attention", "") or "")
    if attention == "very_low":
        return 2
    if attention == "low":
        return 1
    return 0


def render_headline(windows: list[tuple[Any, Any]]) -> str | None:
    """Return the one-line health headline for non-passed windows."""
    active = [
        (provider, window)
        for provider, window in windows
        if str(getattr(window, "reset_state", "") or "") != "passed"
    ]
    if not active:
        return None
    top = max(_headline_level(window) for _, window in active)
    at_level = [(p, w) for p, w in active if _headline_level(w) == top]
    count = len(at_level)
    tightest_provider, tightest_window = min(
        at_level,
        key=lambda item: float(getattr(item[1], "remaining_percent", 100.0) or 100.0),
    )
    emoji = getattr(tightest_provider, "emoji", None) or "•"
    label = format_window_label(tightest_window)
    pct = format_short_percent(
        str(getattr(tightest_window, "remaining_text", "") or "")
    )
    noun = "window" if count == 1 else "windows"
    if top == 3:
        return f"⛔ {count} {noun} exhausted · tightest: {emoji} {html_escape(label)} {html_escape(pct)}"
    if top == 2:
        return f"🔴 {count} {noun} nearly out · tightest: {emoji} {html_escape(label)} {html_escape(pct)}"
    if top == 1:
        return f"🟡 {count} {noun} running low · tightest: {emoji} {html_escape(label)} {html_escape(pct)}"
    return f"✅ All windows healthy · tightest: {emoji} {html_escape(label)} {html_escape(pct)}"


def _format_window_line(
    window: Any, *, now: float, tzinfo: Any, provider_problem: bool
) -> str:
    dot = format_attention_dot(window, provider_problem=provider_problem)
    empty = _is_exhausted(window)
    bar = format_capacity_bar(
        float(getattr(window, "remaining_percent", 0.0) or 0.0), empty=empty
    )
    pct = format_short_percent(str(getattr(window, "remaining_text", "") or ""))
    label = format_window_label(window)
    reset = format_reset_text(window, now=now, tzinfo=tzinfo)
    head = f"{dot} <code>{html_escape(bar)} {html_escape(f'{pct:>4}')}</code>  {html_escape(label)}"
    if reset is not None:
        return f"{head} · ↻ {html_escape(reset)}"
    return head


def _provider_has_problem(provider: Any) -> bool:
    status = str(getattr(provider, "collection_status", "") or "")
    if status in {"error", "unauthenticated"}:
        return True
    collector_state = getattr(provider, "collector_state", None)
    return collector_state in {"degraded", "failing"}


def _format_no_window_row(provider: Any) -> str:
    status = str(getattr(provider, "collection_status", "") or "")
    status_label = str(getattr(provider, "status_label", "") or status)
    retry_label = getattr(provider, "retry_label", None)
    retry = (
        f" · {html_escape(str(retry_label))}"
        if isinstance(retry_label, str) and retry_label
        else ""
    )
    if status == "unauthenticated":
        return "🔒 Logged out"
    if status in {"error"} or getattr(provider, "collector_state", None) in {
        "degraded",
        "failing",
    }:
        return f"⚠️ {html_escape(status_label)}{retry}"
    if status in {"unsupported", "not_applicable"}:
        return f"➖ Not tracked ({html_escape(status_label)})"
    return "⏳ No data yet — tap Refresh"


def build_usage_blocks(
    report: Any,
    *,
    now: float,
    tzinfo: Any,
    status_line: str | None = None,
    scope: str = "all",
) -> list[str]:
    """Return one HTML block per section for a usage report."""
    providers = sorted(
        getattr(report, "providers", ()) or (),
        key=lambda item: str(getattr(item, "provider", "") or ""),
    )
    filtered_emoji: str | None = None
    filtered_name: str | None = None
    if scope != "all" and len(providers) == 1:
        only = providers[0]
        filtered_emoji = str(getattr(only, "emoji", None) or "•")
        filtered_name = str(
            getattr(only, "display_name", "") or getattr(only, "provider", "")
        )
    if filtered_name:
        title = f"📊 <b>Usage</b> · {html_escape(filtered_emoji or '•')} {html_escape(filtered_name)}"
    else:
        title = "📊 <b>Usage</b> · capacity left"
    windows: list[tuple[Any, Any]] = []
    for provider in providers:
        for window in tuple(getattr(provider, "windows", ()) or ()):
            windows.append((provider, window))
    headline = render_headline(windows)
    blocks = [title + (f"\n{headline}" if headline else "")]
    for provider in providers:
        lines = [format_provider_header(provider, now=now)]
        windows_sorted = sorted(
            getattr(provider, "windows", ()) or (), key=_window_sort_key
        )
        problem = _provider_has_problem(provider)
        collector_state = getattr(provider, "collector_state", None)
        retry_label = getattr(provider, "retry_label", None)
        if windows_sorted and collector_state in {"degraded", "failing"}:
            retry = f" · {html_escape(str(retry_label))}" if retry_label else ""
            lines.append(f"⚠️ Collector {html_escape(str(collector_state))}{retry}")
        if not windows_sorted:
            lines.append(_format_no_window_row(provider))
        for window in windows_sorted:
            lines.append(
                _format_window_line(
                    window, now=now, tzinfo=tzinfo, provider_problem=problem
                )
            )
        blocks.append("\n".join(lines))
    try:
        tz = tzinfo if tzinfo is not None else UTC
        checked = datetime.fromtimestamp(float(now), tz=tz).strftime("%H:%M:%S")
    except Exception:
        checked = "unknown"
    footer_lines: list[str] = []
    if status_line:
        footer_lines.append(html_escape(status_line))
    footer_lines.append(f"<i>🕐 Checked {html_escape(checked)}</i>")
    blocks.append("\n".join(footer_lines))
    return blocks


def build_usage_chunks(
    report: Any,
    *,
    now: float,
    tzinfo: Any,
    status_line: str | None = None,
    scope: str = "all",
) -> list[str]:
    """Pack usage blocks into Telegram-sized HTML chunks."""
    return pack_html_blocks(
        build_usage_blocks(
            report, now=now, tzinfo=tzinfo, status_line=status_line, scope=scope
        )
    )
