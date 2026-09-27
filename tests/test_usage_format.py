"""Golden and edge-case tests for the Telegram /usage renderer."""

from __future__ import annotations

from datetime import timezone, UTC
from types import SimpleNamespace

from sase_telegram.usage_format import (
    build_usage_blocks,
    format_attention_dot,
    format_capacity_bar,
    format_short_percent,
    format_window_label,
)


FROZEN_NOW = 1_800_000_000.0
TZ = UTC


def _window(**overrides):
    base = {
        "key": "weekly",
        "label": "Weekly",
        "period": "weekly",
        "duration_seconds": 604800.0,
        "scope": "all_models",
        "scope_family": None,
        "scope_models": (),
        "scope_vendor_label": None,
        "used_percent": 38.0,
        "remaining_percent": 62.0,
        "remaining_text": "62% left",
        "exceeded_by_percent": None,
        "resets_at": FROZEN_NOW + 11760.0,
        "reset_state": "future",
        "seconds_until_reset": 11760.0,
        "freshness": "fresh",
        "age_seconds": 30.0,
        "vendor_state": "allowed",
        "attention": "none",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider(key, **overrides):
    base = {
        "provider": key,
        "display_name": key.title(),
        "emoji": "•",
        "plan": None,
        "collection_status": "ok",
        "status_label": "ok",
        "collector_state": None,
        "retry_label": None,
        "last_observed_at": FROZEN_NOW - 30.0,
        "windows": (),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _report(*providers, enabled=True, configured=("codex",)):
    return SimpleNamespace(
        generated_at=FROZEN_NOW,
        collection_enabled=enabled,
        providers=tuple(providers),
        configured_providers=tuple(configured),
        diagnostics=(),
    )


def test_healthy_multi_provider_golden_shape() -> None:
    provider = _provider(
        "codex",
        display_name="Codex",
        emoji="🤖",
        plan="pro",
        windows=(
            _window(key="weekly", remaining_percent=97.0, remaining_text="97% left"),
        ),
    )
    blocks = build_usage_blocks(_report(provider), now=FROZEN_NOW, tzinfo=TZ)
    assert blocks[0].startswith("📊 <b>Usage</b> · capacity left")
    assert "✅ All windows healthy" in blocks[0]
    assert "🤖" in blocks[1]
    assert "Codex" in blocks[1]
    assert "██████████" in blocks[1]
    assert blocks[-1].startswith("<i>🕐 Checked")


def test_attention_dots_cover_every_level() -> None:
    assert format_attention_dot(_window(attention="rejected")) == "⛔"
    assert (
        format_attention_dot(_window(attention="none", exceeded_by_percent=5.0)) == "⛔"
    )
    assert format_attention_dot(_window(attention="very_low")) == "🔴"
    assert format_attention_dot(_window(attention="low")) == "🟡"
    assert format_attention_dot(_window(attention="none")) == "🟢"
    assert format_attention_dot(_window(attention="none", freshness="stale")) == "⚪"
    assert format_attention_dot(_window(attention="none", reset_state="passed")) == "⚪"
    assert (
        format_attention_dot(_window(attention="none"), provider_problem=True) == "⚪"
    )
    # Attention wins over staleness.
    assert (
        format_attention_dot(_window(attention="very_low", freshness="stale")) == "🔴"
    )


def test_headline_levels_and_pluralization() -> None:
    low = _provider(
        "codex", windows=(_window(attention="low", remaining_percent=20.0),)
    )
    blocks = build_usage_blocks(_report(low), now=FROZEN_NOW, tzinfo=TZ)
    assert "🟡 1 window running low" in blocks[0]
    two = _provider(
        "codex",
        windows=(
            _window(key="a", attention="low", remaining_percent=20.0),
            _window(key="b", attention="low", remaining_percent=10.0),
        ),
    )
    blocks = build_usage_blocks(_report(two), now=FROZEN_NOW, tzinfo=TZ)
    assert "🟡 2 windows running low" in blocks[0]
    exhausted = _provider(
        "codex", windows=(_window(attention="rejected", remaining_percent=0.0),)
    )
    blocks = build_usage_blocks(_report(exhausted), now=FROZEN_NOW, tzinfo=TZ)
    assert "⛔ 1 window exhausted" in blocks[0]
    very_low = _provider(
        "codex", windows=(_window(attention="very_low", remaining_percent=3.0),)
    )
    blocks = build_usage_blocks(_report(very_low), now=FROZEN_NOW, tzinfo=TZ)
    assert "🔴 1 window nearly out" in blocks[0]
    # Passed resets are excluded from the headline.
    passed = _provider(
        "codex",
        windows=(_window(attention="rejected", reset_state="passed"),),
    )
    blocks = build_usage_blocks(_report(passed), now=FROZEN_NOW, tzinfo=TZ)
    assert "⛔" not in blocks[0]


def test_stale_provider_header() -> None:
    provider = _provider(
        "codex",
        windows=(_window(freshness="stale"),),
    )
    blocks = build_usage_blocks(_report(provider), now=FROZEN_NOW, tzinfo=TZ)
    assert "stale" in blocks[1]
    assert "⚠️" in blocks[1]


def test_reset_formatting_variants() -> None:
    soon = _window(resets_at=FROZEN_NOW + 13920.0, seconds_until_reset=13920.0)
    far = _window(resets_at=FROZEN_NOW + 200000.0, seconds_until_reset=200000.0)
    passed = _window(reset_state="passed", resets_at=FROZEN_NOW - 10.0)
    unknown = _window(resets_at=None, reset_state="unknown")
    provider = _provider("codex", windows=(soon, far, passed, unknown))
    blocks = build_usage_blocks(_report(provider), now=FROZEN_NOW, tzinfo=TZ)
    body = blocks[1]
    assert "↻ in 3h 52m" in body
    assert "↻ reset, refreshing…" in body
    # Far-future resets render as weekday + clock.
    assert "↻" in body


def test_label_derivation() -> None:
    assert format_window_label(_window(period="session", scope="all_models")) == "5h"
    assert format_window_label(_window(period="weekly", scope="all_models")) == "Weekly"
    assert (
        format_window_label(
            _window(
                period="duration",
                duration_seconds=18000.0,
                scope="all_models",
            )
        )
        == "5h"
    )
    assert (
        format_window_label(
            _window(period="weekly", scope="models", scope_models=("fable",))
        )
        == "Fable · Weekly"
    )
    assert (
        format_window_label(
            _window(period="weekly", scope="model_family", scope_family="gemini")
        )
        == "Gemini · Weekly"
    )
    assert (
        format_window_label(
            _window(period="weekly", scope="model_family", scope_family="3p")
        )
        == "3p · Weekly"
    )
    long_label = format_window_label(
        _window(
            period="weekly",
            scope="models",
            scope_models=("averylongmodelaliasname",),
        )
    )
    assert long_label.endswith("…")
    assert len(long_label) == 18


def test_bar_edge_cases() -> None:
    assert format_capacity_bar(100.0) == "█" * 10
    assert format_capacity_bar(0.0) == "░" * 10
    assert format_capacity_bar(0.5) == "░" * 10
    assert format_capacity_bar(3.0).count("█") == 1
    assert format_capacity_bar(99.0).count("█") == 10
    assert format_capacity_bar(62.0).count("█") == 6
    assert format_capacity_bar(62.0, empty=True) == "░" * 10
    assert format_short_percent("62% left") == "62%"
    assert format_short_percent("<1% left") == "<1%"


def test_no_window_status_rows() -> None:
    for status, marker in [
        ("unauthenticated", "🔒 Logged out"),
        ("error", "⚠️"),
        ("unsupported", "➖ Not tracked"),
        ("not_applicable", "➖ Not tracked"),
        ("no_observations", "⏳ No data yet"),
    ]:
        provider = _provider(
            "codex",
            collection_status=status,
            status_label=status.replace("_", " "),
            collector_state="degraded" if status == "error" else None,
            retry_label="retry in 5m" if status == "error" else None,
        )
        blocks = build_usage_blocks(_report(provider), now=FROZEN_NOW, tzinfo=TZ)
        assert marker in blocks[1], status


def test_html_escaping_of_hostile_labels() -> None:
    provider = _provider(
        "codex",
        display_name="<b>evil</b>",
        windows=(_window(label="<script>Weekly</script>"),),
    )
    blocks = build_usage_blocks(_report(provider), now=FROZEN_NOW, tzinfo=TZ)
    assert "<script>" not in blocks[1]
    assert "&lt;script&gt;" in blocks[1]
    assert "<b>evil</b>" not in blocks[1]
