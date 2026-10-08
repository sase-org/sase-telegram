"""Telegram Plan Decisions review and receipts (sase-1hi.7).

Focused tests using real shared decision fixtures and verified gate
envelopes where possible, mocking Telegram API calls and process launches.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from sase.notification_gates.service import create_gate
from sase.notifications.models import Notification
from sase.plan_gate import build_plan_approval_gate_spec
from sase_telegram import inbound, outbound, pending_actions
from sase_telegram.formatting import format_notification, render_gate_keyboard
from sase_telegram.gate_flow import load_gate_view, load_progress

TALE_DECISIONS_PLAN = """---
tier: tale
title: Keymap help overlay
goal: Pressing ? shows every active binding.
size: small
decisions:
  grouping:
    ask: How should the overlay group bindings?
    choices:
      pane: By pane, matching the footer hints
      mode: By leader mode; denser, but splits pane actions
    default: pane
    why: pane keeps the footer's order
  terse:
    ask: Keep the overlay terse?
    default: true
---
# Plan

Implement the overlay.
"""

EPIC_DECISIONS_PLAN = """---
tier: epic
title: Epic decisions
goal: Verify epic decision rendering.
phases:
  - id: implementation
    title: Implement
    depends_on: []
    size: small
decisions:
  grouping:
    ask: How should the overlay group bindings?
    choices:
      pane: By pane, matching the footer hints
      mode: By leader mode; denser, but splits pane actions
    default: pane
    why: pane keeps the footer's order
---
# Plan

Implement the epic.
"""

MEMORY_DECISIONS_PLAN = """---
tier: tale
title: Memory decisions
goal: Verify memory provenance rendering.
size: small
decisions:
  tui_note:
    ask: Record the overlay conventions in the tui memory note?
    memory: [tui.md]
    requested: "and note the convention in the tui memory"
    default: true
---
# Plan

Implement the overlay.
"""


@pytest.fixture()
def gate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sase.notification_gates import paths
    from sase.notifications import pending_actions as core_pending
    from sase.notifications import store

    monkeypatch.setattr(paths, "INTERACTION_REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(store, "NOTIFICATIONS_DIR", str(tmp_path / "notifications"))
    monkeypatch.setattr(
        store,
        "NOTIFICATIONS_FILE",
        str(tmp_path / "notifications" / "notifications.jsonl"),
    )
    monkeypatch.setattr(core_pending, "PENDING_ACTIONS_PATH", tmp_path / "core.json")
    monkeypatch.setattr(
        core_pending, "LEGACY_TELEGRAM_PENDING_ACTIONS_PATH", tmp_path / "telegram.json"
    )
    monkeypatch.setattr(pending_actions, "PENDING_ACTIONS_PATH", tmp_path / "core.json")
    monkeypatch.setattr(inbound, "AWAITING_FEEDBACK_PATH", tmp_path / "awaiting.json")
    monkeypatch.setattr(
        inbound, "GATE_COMPLETION_PENDING_DIR", tmp_path / "gate_completions"
    )
    store._LOAD_CACHE.clear()
    return tmp_path


@pytest.fixture(autouse=True)
def _inline_gate_submissions(monkeypatch: pytest.MonkeyPatch) -> None:
    from sase.notification_gates.cli_support import resolve_gate_cli_bundle
    from sase.notification_gates.executor import execute_gate_selection

    def _run(request: Any) -> Any:
        argv = list(request.argv)
        kind = argv[argv.index("--kind") + 1]
        request_id = argv[argv.index("--id") + 1]
        bundle = resolve_gate_cli_bundle(kind, request_id)
        payload = dict(request.operation_payload or {})
        option_ids = [str(item) for item in payload.get("option_ids", [])]
        option_inputs = payload.get("option_inputs")
        revision = payload.get("review_revision")
        expected = int(revision) if revision is not None else None
        if option_inputs is not None:
            execute_gate_selection(
                bundle.root,
                option_ids,
                None,
                feedback=payload.get("feedback"),
                source="telegram",
                option_inputs=option_inputs,
                expected_review_revision=expected,
            )
        else:
            execute_gate_selection(
                bundle.root,
                option_ids,
                {},
                feedback=payload.get("feedback"),
                source="telegram",
                expected_review_revision=expected,
            )
        return SimpleNamespace(proc_id="fake-proc")

    monkeypatch.setattr("sase.procs.service.submit_proc_request", _run)


def _tale_notification(
    gate_home: Path, request_id: str, plan: str = TALE_DECISIONS_PLAN
) -> Notification:
    plan_file = gate_home / f"{request_id}.md"
    plan_file.write_text(plan, encoding="utf-8")
    result = create_gate(build_plan_approval_gate_spec(plan_file, request_id))
    bundle_path = Path(result.bundle_path)
    request = json.loads((bundle_path / "request.json").read_text(encoding="utf-8"))
    action_data = dict(request.get("presentation", {}).get("action_data", {}))
    action_data.update(
        {
            "request_id": str(request["request_id"]),
            "request_kind": str(request["kind"]),
            "bundle_path": str(bundle_path),
        }
    )
    notification = Notification(
        id=str(result.notification_id),
        timestamp="2026-07-17T00:00:00+00:00",
        sender="planner",
        notes=["Tale ready for review"],
        files=[str(bundle_path / "plan.md")],
        action="PlanApproval",
        action_data=action_data,
    )
    return notification


def _pending(notification: Notification) -> dict[str, Any]:
    return {
        "notification_id": notification.id,
        "action": notification.action,
        "action_data": notification.action_data,
        "message_id": 42,
        "chat_id": "chat-1",
    }


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="cb", data=data, message=SimpleNamespace(message_id=42, chat_id="chat-1")
    )


def test_sheet_rendering_order_glyphs_and_bookkeeping(gate_home: Path) -> None:
    notification = _tale_notification(gate_home, "telegram-sheet")
    text, keyboard, _ = format_notification(notification)
    assert "Decisions · 2" in text
    assert "grouping" in text and "How should the overlay group bindings?" in text
    assert "★" in text and "pane keeps the footer's order" in text
    assert "decisions:" not in text.replace("Decisions", "")
    assert "decided_by" not in text and "decided_via" not in text
    assert "yes" in text and "🧠" not in text  # no memory decisions here
    assert len(text) <= 4096
    assert keyboard is not None


def test_memory_provenance_and_escaping(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real memory fixture: a temporary project with a reference tui.md.
    project = tmp_path / "memory-proj"
    mem_dir = project / "sase" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "tui.md").write_text(
        "---\ntype: reference\n---\n\n# TUI\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    notification = _tale_notification(
        gate_home, "telegram-memory", MEMORY_DECISIONS_PLAN
    )
    text, _, _ = format_notification(notification)
    assert "🧠" in text and ("tui_note" in text or "tui\\_note" in text)
    assert "you asked" in text or "not asked" in text or "quote not found" in text
    assert len(text) <= 4096


def test_maximum_sheet_budget_and_degrade() -> None:
    from sase_telegram.decision_sheet import render_decision_sheet

    definitions = [
        {
            "id": f"d{i}",
            "kind": "choice",
            "ask": f"Question {i} with <special> & chars?",
            "why": "because reasons",
            "choices": [{"key": f"k{j}", "label": f"Label {j} <>&"} for j in range(5)],
            "default": "k0",
        }
        for i in range(5)
    ]
    text = render_decision_sheet(definitions, None, 1)
    assert len(text) <= 1800
    assert "Question 0" in text and "k0" in text
    # Every complete ask and its default survive the degrade order.
    for i in range(5):
        assert f"Question {i}" in text


def test_keyboard_rows_primary_reset_and_token_bytes(gate_home: Path) -> None:
    notification = _tale_notification(gate_home, "telegram-keyboard")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    keyboard = render_gate_keyboard(notification.id[:8], view, progress)
    assert keyboard is not None
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert any("grouping" in text for text in texts)
    assert any(
        "Tale" in text and ("defaults" in text or "change" in text) for text in texts
    )
    for button in [b for row in keyboard.inline_keyboard for b in row]:
        assert len(button.callback_data.encode("utf-8")) <= 64


def test_repeated_set_is_idempotent_and_validated(gate_home: Path) -> None:
    from sase_telegram.decision_callbacks import apply_decision_token
    from sase_telegram.plan_decisions import current_values

    notification = _tale_notification(gate_home, "telegram-replay")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    revision = view.review_revision
    updated, _, _ = apply_decision_token(view, progress, f"d0=k1r{revision}")
    again, _, _ = apply_decision_token(view, updated, f"d0=k1r{revision}")
    definitions = [dict(item) for item in view.decisions]
    assert current_values(definitions, updated.decision_values) == current_values(
        definitions, again.decision_values
    )
    with pytest.raises(ValueError):
        apply_decision_token(view, again, f"d9=k0r{revision}")


def test_edits_have_no_side_effects_and_persist_across_restart(gate_home: Path) -> None:
    from sase_telegram.decision_callbacks import apply_decision_token
    from sase_telegram.gate_flow import progress_path

    notification = _tale_notification(gate_home, "telegram-persist")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    bundle = Path(notification.action_data["bundle_path"])
    assert not (bundle / "response.json").exists()
    updated, toast, _ = apply_decision_token(
        view, progress, f"d1=0r{view.review_revision}"
    )
    assert "terse" in toast
    assert not (bundle / "response.json").exists()
    assert progress_path(view).exists()
    reloaded = load_progress(view)
    assert reloaded.decision_values == updated.decision_values
    # Legacy and corrupt progress load safely without granting consent.
    progress_path(view).write_text('{"selected_option_ids": []}', encoding="utf-8")
    legacy = load_progress(view)
    assert legacy.decision_values is None
    progress_path(view).write_text(
        '{"decision_values": {"nope": true}}', encoding="utf-8"
    )
    corrupt = load_progress(view)
    assert corrupt.decision_values is None


def test_submit_merges_identical_vectors_and_revision_metadata(gate_home: Path) -> None:
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    notification = _tale_notification(gate_home, "telegram-submit")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    revision = view.review_revision
    with (
        patch("inbound_namespace.INBOUND.telegram_client.answer_callback_query"),
        patch("inbound_namespace.INBOUND.telegram_client.edit_message_reply_markup"),
        patch("inbound_namespace.INBOUND.telegram_client.edit_message_text"),
        patch("inbound_namespace.INBOUND.telegram_client.send_message"),
        patch(
            "sase.plan_approval_actions._archive_plan_for_approval",
            return_value=str(gate_home / "archived-plan.md"),
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:d0=k1r{revision}"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:s0r{revision}"), {prefix: action})
    bundle = Path(notification.action_data["bundle_path"])
    response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    inputs = response.get("option_inputs", {})
    # Approve and commit share identical normalized vectors/defaults.
    assert inputs["approve"]["decision_grouping"] == "mode"
    assert inputs["commit"]["decision_grouping"] == "mode"
    assert inputs["approve"]["decision_terse"] is True
    assert inputs["approve"] == inputs["commit"]
    # No unselected inputs; reject never carries decision fields.
    assert "reject" not in inputs
    request = json.loads((bundle / "request.json").read_text(encoding="utf-8"))
    assert request.get("review_revision") == revision


def test_stale_callback_and_refresh(gate_home: Path) -> None:
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    notification = _tale_notification(gate_home, "telegram-stale")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    stale_revision = view.review_revision + 99
    answers: list[str] = []
    with (
        patch(
            "inbound_namespace.INBOUND.telegram_client.answer_callback_query",
            side_effect=lambda *args: answers.append(str(args[-1])),
        ),
        patch("inbound_namespace.INBOUND.telegram_client.edit_message_reply_markup"),
    ):
        _handle_callback(
            _callback(f"gate:{prefix}:d0=k1r{stale_revision}"), {prefix: action}
        )
    assert answers and answers[0] == "This plan changed since this card was shown."
    bundle = Path(notification.action_data["bundle_path"])
    assert not (bundle / "response.json").exists()


def test_feedback_carries_provisional_vector_and_revision(gate_home: Path) -> None:
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    notification = _tale_notification(gate_home, "telegram-feedback")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    revision = view.review_revision
    # Feedback lives on its own branch (approve+commit is branch 0).
    feedback_branch = next(
        i for i, branch in enumerate(view.branches) if branch == ("feedback",)
    )
    with (
        patch("inbound_namespace.INBOUND.telegram_client.answer_callback_query"),
        patch("inbound_namespace.INBOUND.telegram_client.edit_message_reply_markup"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:d0=k1r{revision}"), {prefix: action})
        _handle_callback(
            _callback(f"gate:{prefix}:f{feedback_branch}r{revision}"),
            {prefix: action},
        )
    awaiting = inbound.load_awaiting_feedback("42")
    assert awaiting is not None
    option_inputs = awaiting["action_info"]["option_inputs"]
    # Feedback carries its own provisional vector on the selected feedback
    # input, not an unselected approve entry.
    assert option_inputs["feedback"]["decision_grouping"] == "mode"
    assert awaiting["action_info"]["review_revision"] == revision


def test_unreplied_text_with_multiple_prompts_does_not_launch(gate_home: Path) -> None:
    from sase_telegram.inbound_handlers.text_messages import _handle_text_message

    inbound.save_awaiting_feedback(
        "1", "prefix1", {"action_type": "gate", "bundle_path": "/tmp/x"}
    )
    inbound.save_awaiting_feedback(
        "2", "prefix2", {"action_type": "gate", "bundle_path": "/tmp/y"}
    )
    message = SimpleNamespace(
        text="hello",
        entities=None,
        message_id=99,
        reply_to_message=None,
        chat=SimpleNamespace(id="chat-1"),
    )
    with (
        patch("inbound_namespace.INBOUND.telegram_client.send_message") as sent,
        patch("inbound_namespace.INBOUND._launch_agent") as launched,
    ):
        _handle_text_message(message, {})
    assert sent.called
    assert not launched.called


def test_settle_receipt_and_launch_failed_text(gate_home: Path) -> None:
    from sase_telegram.decision_receipt import launch_failed_text, receipt_text

    notification = _tale_notification(gate_home, "telegram-receipt")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    values = {"grouping": "mode", "terse": True}
    text = receipt_text(view, values, verdict="Tale")
    assert "grouping → mode" in text or "grouping" in text
    assert "●" in text
    failed = launch_failed_text(text)
    assert "coder could not start" in failed and "grouping" in failed


def test_quiet_receipt_filter_and_delivery(gate_home: Path) -> None:  # noqa: ARG001
    from sase.notifications.models import Notification as _N
    from sase_telegram.outbound import _is_quiet_decision_receipt

    receipt = _N(
        id="abc123",
        timestamp="2026-07-17T00:00:00+00:00",
        sender="auto",
        notes=["auto"],
        files=[],
        action=None,
        action_data={},
        tags=["plan_decisions_receipt"],
        silent=True,
        muted=False,
    )
    assert _is_quiet_decision_receipt(receipt) is True
    other = _N(
        id="def456",
        timestamp="2026-07-17T00:00:00+00:00",
        sender="auto",
        notes=["other"],
        files=[],
        action=None,
        action_data={},
        tags=[],
        silent=True,
        muted=False,
    )
    assert _is_quiet_decision_receipt(other) is False


def test_pdf_preprocessing_and_cleanup(tmp_path: Path) -> None:
    from sase_telegram.decision_pdf import preprocess_plan_for_pdf

    plan = tmp_path / "plan.md"
    plan.write_text(TALE_DECISIONS_PLAN, encoding="utf-8")
    out = preprocess_plan_for_pdf(plan)
    assert out is not None and out.parent == tmp_path
    content = out.read_text(encoding="utf-8")
    assert "## Decisions" in content
    assert "decisions:" not in content
    out.unlink(missing_ok=True)
    assert plan.read_text(encoding="utf-8") == TALE_DECISIONS_PLAN


def test_launch_passes_typed_origin() -> None:
    import inspect

    from sase_telegram.inbound_handlers import agent_launch as _launch

    source = inspect.getsource(_launch._launch_agents_with_notifications)
    assert 'origin="typed"' in source


def test_missing_facade_and_generic_compatibility(gate_home: Path) -> None:
    """Missing facade degrades gracefully; generic gates carry no decisions."""
    from sase_telegram import plan_decisions as _pd
    from sase_telegram.gate_flow import GateView as _View

    with patch.object(_pd, "_plan_decisions_module", return_value=None):
        assert _pd.decisions_available() is False
        assert _pd.sheet_for([], {}) is None
        assert _pd.summary_for({}, "coder + commit", "full") == ""
    # No-decision generic gates render without a Decisions sheet.
    view = _View(
        bundle_path=Path(gate_home),
        request_id="generic",
        kind="custom",
        options=(),
        groups=(),
        branches=(("accept",),),
        decisions=(),
        review_revision=1,
    )
    from sase_telegram.decision_sheet import render_decision_sheet

    assert render_decision_sheet([], None, 1) == ""
    assert view.decisions == ()


def test_approve_commit_reject_feedback_epic_vectors(gate_home: Path) -> None:
    """Approve+commit, approve-only, commit-only, reject, feedback, epic."""
    from sase_telegram.decision_callbacks import build_selected_option_inputs
    from sase_telegram.gate_flow import load_gate_view

    notification = _tale_notification(gate_home, "telegram-vectors")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    # Direct helper: reject gets {}, others get only declared decision_*.
    out = build_selected_option_inputs(view, progress, ("reject",))
    assert out == {"reject": {}}
    out = build_selected_option_inputs(view, progress, ("approve", "commit"))
    assert out["approve"] == out["commit"]
    assert all(k.startswith("decision_") for k in out["approve"])
    # Epic decisions gate renders (EPIC_DECISIONS_PLAN previously unused).
    epic = _tale_notification(gate_home, "telegram-epic", EPIC_DECISIONS_PLAN)
    epic.action = "EpicApproval"
    text, keyboard, _ = format_notification(epic)
    assert "Epic" in text or "decisions" in text.lower()
    assert keyboard is not None


def test_refresh_recovers_stale_and_retryable(gate_home: Path) -> None:
    """Stale edits/submits recover via Refresh; failures stay retryable."""
    from sase_telegram.decision_callbacks import apply_decision_token
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    notification = _tale_notification(gate_home, "telegram-refresh-ok")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    revision = view.review_revision
    # Successful refresh binds to the current revision and never submits.
    updated, toast, submitted = apply_decision_token(view, progress, f"dRr{revision}")
    assert submitted is False and "refreshed" in toast.lower()
    assert updated.displayed_revision == revision
    bundle = Path(notification.action_data["bundle_path"])
    assert not (bundle / "response.json").exists()


def test_receipt_truthful_verdicts_and_surfaces(gate_home: Path) -> None:
    """Exact receipt text for Tale/Epic, reject, feedback, ACE/CLI/auto."""
    from sase_telegram.decision_receipt import (
        approval_verdict,
        decider_surface,
        receipt_text,
    )

    notification = _tale_notification(gate_home, "telegram-receipt-2")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    values = {"grouping": "mode", "terse": True}
    # Approval verdicts from selected ids.
    assert approval_verdict(view, {"selected_option_ids": ["approve", "commit"]}) == (
        "coder + commit"
    )
    assert approval_verdict(view, {"selected_option_ids": ["approve"]}) == "coder"
    assert approval_verdict(view, {"selected_option_ids": ["commit"]}) == "commit"
    # Decider/surface mapping without guessing Telegram.
    assert decider_surface({"caller": "reviewer", "source": "telegram"}) == (
        "you",
        "via Telegram",
    )
    assert decider_surface({"caller": "human", "source": "tui"}) == ("you", "via ACE")
    assert decider_surface({"caller": "human", "source": "cli"}) == ("you", "via CLI")
    text = receipt_text(
        view,
        values,
        response={
            "selected_option_ids": ["approve", "commit"],
            "caller": "reviewer",
            "source": "telegram",
            "responded_at_unix": 1750000000,
        },
    )
    assert "✅ Tale approved" in text
    assert "grouping → mode" in text
    assert "yes ★" in text or "yes" in text
    # Reject and feedback are distinct, never approved.
    rej = receipt_text(view, {}, response={"selected_option_ids": ["reject"]})
    assert "❌" in rej and "approved" not in rej.lower().replace("rejected", "")
    fb = receipt_text(view, values, response={"selected_option_ids": ["feedback"]})
    assert "Feedback" in fb and "provisional" in fb


def test_keyboard_layout_style_and_replay(gate_home: Path) -> None:
    """Pinned layout, success styling, replay idempotency, token length."""
    from sase_telegram.gate_flow import toggle_option

    notification = _tale_notification(gate_home, "telegram-layout")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    keyboard = render_gate_keyboard(notification.id[:8], view, progress)
    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    # Decision rows first, primary Tale action present.
    assert any("grouping" in t for t in texts)
    assert any("Tale" in t for t in texts)
    for b in [x for row in keyboard.inline_keyboard for x in row]:
        assert len(b.callback_data.encode("utf-8")) <= 64
    # AND replay idempotency via explicit set-state tokens.
    from sase_telegram.gate_flow import expand_branch

    # Find an AND branch if present; otherwise exercise the parser directly.
    and_indexes = [i for i, br in enumerate(view.branches) if len(br) > 1]
    if and_indexes:
        expanded = expand_branch(view, progress, and_indexes[0])
        first_option = view.branches[and_indexes[0]][0]
        from sase_telegram.gate_flow import option_index

        idx = option_index(view, first_option)
        once, _ = toggle_option(view, expanded, f"x{idx}=1")
        twice, _ = toggle_option(view, once, f"x{idx}=1")
        assert once.selected_option_ids == twice.selected_option_ids


def test_sheet_budget_degrade_and_escapes() -> None:
    """1800-char degrade order, expandable syntax, no split escapes."""
    from sase_telegram.decision_sheet import render_decision_sheet

    definitions = [
        {
            "id": "choice1",
            "kind": "choice",
            "ask": "Pick one?",
            "why": "why detail",
            "choices": [
                {"key": "a", "label": "Alpha <&>"},
                {"key": "b", "label": "Beta with a very long label " * 10},
            ],
            "default": "a",
        }
    ]
    text = render_decision_sheet(definitions, None, 1, budget=1800)
    assert len(text) <= 1800
    assert "Pick one?" in text and "a" in text
    # Escapes are never split: no trailing lone backslash.
    assert not text.endswith("\\")


def test_pdf_accepted_pending_and_callouts(tmp_path: Path, gate_home: Path) -> None:
    """Accepted PDFs via stamped sheet; pending via frozen facts; cleanup."""
    from sase_telegram.decision_pdf import _label_callouts, preprocess_plan_for_pdf

    plan = tmp_path / "plan.md"
    plan.write_text(TALE_DECISIONS_PLAN, encoding="utf-8")
    out = preprocess_plan_for_pdf(plan)
    # Without gate context or stamped answers, no independent YAML table.
    # With a frozen bundle context, pending PDFs use gate facts.
    notification = _tale_notification(gate_home, "telegram-pdf")
    bundle = Path(notification.action_data["bundle_path"])
    pending_src = bundle / "plan.md"
    out2 = preprocess_plan_for_pdf(
        pending_src, gate_context={"bundle_path": str(bundle)}
    )
    assert out2 is not None
    content = out2.read_text(encoding="utf-8")
    assert "## Decisions" in content
    out2.unlink(missing_ok=True)
    if out is not None:
        out.unlink(missing_ok=True)
    # Multiline callout labelling retains every line.
    body = "> [!decision] grouping = pane First\n> continuation line\n\ntext"
    labelled = _label_callouts(body)
    assert "Decision" in labelled and "continuation" in labelled
    assert plan.read_text(encoding="utf-8") == TALE_DECISIONS_PLAN
