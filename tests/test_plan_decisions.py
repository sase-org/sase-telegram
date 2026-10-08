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
    """Helper vectors only: reject empties, approve+commit shares one vector.

    Full Reject, approve-only, and commit-only callback submits live in
    ``test_submit_reject_approve_only_commit_only``; the epic card verdict
    lives in ``test_epic_primary_action_and_verdict``.
    """
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


def test_receipt_headers_single_attribution_all_surfaces(gate_home: Path) -> None:
    """One provenance phrase on approve/reject/feedback across surfaces."""
    from sase_telegram.decision_receipt import (
        format_attribution,
        normalize_surface,
        receipt_text,
    )

    notification = _tale_notification(gate_home, "telegram-provenance")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    values = {"grouping": "mode", "terse": True}
    cases = [
        ({"caller": "reviewer", "source": "telegram"}, "you via Telegram"),
        ({"caller": "human", "source": "tui"}, "you via ACE"),
        ({"caller": "human", "source": "cli"}, "you via CLI"),
        ({"caller": "human", "source": "mobile"}, "you via mobile"),
        ({"caller": "agent", "source": "cli"}, "agent via CLI"),
        ({"caller": "auto", "source": "auto_resolution"}, "auto"),
    ]
    outcomes = [
        (["approve", "commit"], "✅ Tale approved"),
        (["reject"], "❌ Rejected"),
        (["feedback"], "💬 Feedback sent"),
    ]
    for provenance, attribution in cases:
        for selected, marker in outcomes:
            response = {
                "selected_option_ids": selected,
                **provenance,
                "responded_at_unix": 1750000000,
            }
            text = receipt_text(view, dict(values), response=dict(response))
            first = text.splitlines()[0]
            assert "via via" not in first, first
            assert "auto auto" not in first, first
            assert first.count("via") <= 1, first
            if attribution == "auto" and selected == ["approve", "commit"]:
                assert first.startswith("🤖 Auto-approved tale"), first
            else:
                assert first.startswith(f"{marker} · {attribution}"), first
    # Shared helper never doubles explicit `via` inputs either.
    assert format_attribution("you", "via Telegram") == "you via Telegram"
    assert format_attribution("you", "Telegram") == "you via Telegram"
    assert format_attribution("human", "via mobile") == "human via mobile"
    assert format_attribution("auto", "auto") == "auto"
    assert normalize_surface("via via Telegram") == "via Telegram"
    assert normalize_surface("via ACE") == "via ACE"
    assert normalize_surface("Telegram") == "via Telegram"
    assert normalize_surface("auto") == "auto"
    # Explicit doubled inputs collapse to one phrase on every outcome.
    doubled = receipt_text(
        view,
        dict(values),
        response={
            "selected_option_ids": ["reject"],
            "caller": "reviewer",
            "source": "telegram",
            "responded_at_unix": 1750000000,
        },
        decider="you",
        surface="via via Telegram",
    )
    assert "via via" not in doubled.splitlines()[0]
    # Epic wording names Epic, never Tale.
    epic = _tale_notification(
        gate_home, "telegram-provenance-epic", EPIC_DECISIONS_PLAN
    )
    epic_view = load_gate_view(epic.action_data)
    epic_text = receipt_text(
        epic_view,
        {"grouping": "mode"},
        response={
            "selected_option_ids": ["approve"],
            "caller": "reviewer",
            "source": "telegram",
            "responded_at_unix": 1750000000,
        },
    )
    assert epic_text.splitlines()[0].startswith("✅ Epic approved · you via Telegram")
    epic_auto = receipt_text(
        epic_view,
        {"grouping": "pane"},
        response={
            "selected_option_ids": ["approve"],
            "caller": "auto",
            "source": "auto_resolution",
            "responded_at_unix": 1750000000,
        },
    )
    assert epic_auto.splitlines()[0].startswith("🤖 Auto-approved epic")


def test_submit_reject_approve_only_commit_only(gate_home: Path) -> None:
    """Real Reject, approve-only, and commit-only callbacks on a decision plan."""
    from sase_telegram.decision_callbacks import _declared_decision_keys
    from sase_telegram.gate_flow import load_progress, option_index
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    def _submit_single(request_id: str, select: list[str]) -> dict[str, Any]:
        notification = _tale_notification(gate_home, request_id)
        prefix = notification.id[:8]
        action = _pending(notification)
        pending_actions.add(prefix, action)
        view = load_gate_view(notification.action_data, expected_kind="plan")
        revision = view.review_revision
        branch_of = {
            "approve": 0,
            "commit": 0,
            "reject": 1,
            "feedback": 2,
        }
        with (
            patch("inbound_namespace.INBOUND.telegram_client.answer_callback_query"),
            patch(
                "inbound_namespace.INBOUND.telegram_client.edit_message_reply_markup"
            ),
            patch("inbound_namespace.INBOUND.telegram_client.edit_message_text"),
            patch("inbound_namespace.INBOUND.telegram_client.send_message"),
            patch(
                "sase.plan_approval_actions._archive_plan_for_approval",
                return_value=str(gate_home / "archived-plan.md"),
            ),
        ):
            if select == ["reject"]:
                _handle_callback(
                    _callback(f"gate:{prefix}:c{branch_of['reject']}r{revision}"),
                    {prefix: action},
                )
            else:
                _handle_callback(
                    _callback(f"gate:{prefix}:c0r{revision}"), {prefix: action}
                )
                # Branch 0 defaults to approve+commit; switch the other
                # member off explicitly for single-verdict submits.
                for option_id in ("approve", "commit"):
                    if option_id not in select:
                        idx = option_index(view, option_id)
                        _handle_callback(
                            _callback(f"gate:{prefix}:x{idx}=0r{revision}"),
                            {prefix: action},
                        )
                _handle_callback(
                    _callback(f"gate:{prefix}:s0r{revision}"), {prefix: action}
                )
        bundle = Path(notification.action_data["bundle_path"])
        response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
        assert sorted(str(s) for s in response["selected_option_ids"]) == sorted(select)
        inputs = response.get("option_inputs", {})
        for option_id in select:
            declared = _declared_decision_keys(view, option_id)
            if option_id == "reject":
                assert inputs.get(option_id, {}) == {}
            else:
                assert set(inputs[option_id]) == declared
                assert declared, "decision options must declare decision_* fields"
                assert all(k.startswith("decision_") for k in inputs[option_id])
        for option_id in inputs:
            assert option_id in select, "no unselected inputs are submitted"
        # Displayed revision travels with the submission.
        assert (
            response.get("review_revision", revision) == revision
            or json.loads((bundle / "request.json").read_text(encoding="utf-8")).get(
                "review_revision"
            )
            == revision
        )
        # Recovery context is retained for the completion poll.
        progress = load_progress(view)
        assert progress.submitted_revision == revision
        assert progress.submitted_values is not None
        assert progress.source_message_id == 42
        return response

    rej = _submit_single("telegram-submit-reject", ["reject"])
    assert rej["selected_option_ids"] == ["reject"]
    approve_only = _submit_single("telegram-submit-approve-only", ["approve"])
    assert approve_only["option_inputs"]["approve"]["decision_grouping"] in (
        "pane",
        "mode",
    )
    assert "commit" not in approve_only.get("option_inputs", {})
    commit_only = _submit_single("telegram-submit-commit-only", ["commit"])
    assert commit_only["option_inputs"]["commit"]["decision_grouping"] in (
        "pane",
        "mode",
    )
    assert "approve" not in commit_only.get("option_inputs", {})


def _deferred_submit_context(
    gate_home: Path, request_id: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Build a submitted-but-unanswered decision bundle with a draft.

    The gate proc stays deferred (no synchronous fake execution) so error
    and missing-output recovery is exercised through the durable records,
    never through the inline fake.
    """
    from dataclasses import replace

    from sase_telegram.decision_callbacks import apply_decision_token
    from sase_telegram.gate_flow import save_progress as save_gate_progress

    monkeypatch.setattr(
        "sase.procs.service.submit_proc_request",
        lambda request: SimpleNamespace(proc_id=f"deferred-{request_id}"),
    )
    notification = _tale_notification(gate_home, request_id)
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    revision = view.review_revision
    progress = load_progress(view)
    updated, _, _ = apply_decision_token(view, progress, f"d0=k1r{revision}")
    updated = replace(
        updated,
        source_message_id=42,
        source_chat_id="chat-1",
        displayed_revision=revision,
        submitted_revision=revision,
        submitted_values=dict(updated.decision_values or {}),
    )
    save_gate_progress(view, updated)
    bundle = Path(notification.action_data["bundle_path"])
    created = 1000.0
    record = {
        "prefix": prefix,
        "proc_id": f"deferred-{request_id}",
        "request_id": request_id,
        "kind": "plan",
        "bundle_path": str(bundle),
        "chat_id": "chat-1",
        "created_at": created,
    }
    return {
        "notification": notification,
        "prefix": prefix,
        "action": action,
        "view": view,
        "revision": revision,
        "bundle": bundle,
        "created": created,
        "record": record,
    }


def _write_completion_record(tmp_path: Path, record: dict[str, Any]) -> Path:
    pending_path = tmp_path / f"{record['proc_id']}.json"
    pending_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return pending_path


def test_deferred_stale_error_restores_refresh_and_keeps_draft(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred stale rejection sends the exact text and keeps recovery state."""
    import sase_telegram.inbound_handlers.gate_completions as gc
    from sase_telegram.plan_decisions import STALE_TEXT

    ctx = _deferred_submit_context(gate_home, "telegram-deferred-stale", monkeypatch)
    bundle, revision, record = ctx["bundle"], ctx["revision"], ctx["record"]
    errors = bundle / "errors"
    errors.mkdir(parents=True, exist_ok=True)
    (errors / "stale.json").write_text(
        json.dumps(
            {
                "code": "stale_review",
                "message": f"stale_review: current revision is {revision}",
                "created_at_unix": ctx["created"] + 1,
            }
        ),
        encoding="utf-8",
    )
    pending_path = _write_completion_record(tmp_path, dict(record))
    sent: list[str] = []
    markups: list[Any] = []
    monkeypatch.setattr(gc, "_gate_answer_proc_status", lambda proc_id: None)
    with (
        patch.object(
            gc.telegram_client, "send_message", side_effect=lambda c, t: sent.append(t)
        ),
        patch.object(
            gc.telegram_client,
            "edit_message_reply_markup",
            side_effect=lambda c, m, reply_markup=None: markups.append(reply_markup),
        ),
    ):
        assert (
            gc._settle_decision_receipt(
                pending_path,
                json.loads(pending_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + 2,
            )
            is True
        )
    assert sent == [STALE_TEXT]
    assert markups and any(
        "dRr" in (b.callback_data or "")
        for markup in markups
        for row in markup.inline_keyboard
        for b in row
    )
    assert any(
        "Refresh" in b.text
        for markup in markups
        for row in markup.inline_keyboard
        for b in row
    )
    # Draft, displayed revision, pending action, and progress are kept.
    reloaded = load_progress(ctx["view"])
    assert reloaded.decision_values == {"grouping": "mode"}
    assert reloaded.displayed_revision == revision
    assert (bundle / "telegram_gate_progress.json").exists()
    assert pending_actions.get(ctx["prefix"]) is not None
    assert not (bundle / "response.json").exists()
    assert not pending_path.exists()
    # Tapping Refresh reloads prose and keeps only still-valid draft values.
    from sase_telegram.decision_callbacks import apply_decision_token

    refreshed, toast, submitted = apply_decision_token(
        ctx["view"], reloaded, f"dRr{revision}"
    )
    assert submitted is False
    assert refreshed.displayed_revision == revision
    assert refreshed.decision_values == {"grouping": "mode"}
    assert "refresh" in toast.lower()


def test_recorded_error_reports_message_and_stays_retryable(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded schema rejection reports its message with controls usable."""
    import sase_telegram.inbound_handlers.gate_completions as gc

    ctx = _deferred_submit_context(gate_home, "telegram-deferred-schema", monkeypatch)
    bundle, record = ctx["bundle"], ctx["record"]
    errors = bundle / "errors"
    errors.mkdir(parents=True, exist_ok=True)
    message = "choice key 'zzz' is not allowed for decision grouping"
    (errors / "schema.json").write_text(
        json.dumps(
            {
                "code": "decision-resolve-failed",
                "message": message,
                "created_at_unix": ctx["created"] + 1,
            }
        ),
        encoding="utf-8",
    )
    pending_path = _write_completion_record(tmp_path, dict(record))
    sent: list[str] = []
    monkeypatch.setattr(gc, "_gate_answer_proc_status", lambda proc_id: None)
    with (
        patch.object(
            gc.telegram_client, "send_message", side_effect=lambda c, t: sent.append(t)
        ),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert (
            gc._settle_decision_receipt(
                pending_path,
                json.loads(pending_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + 2,
            )
            is True
        )
    assert sent == [message]
    assert "finished without a recorded answer" not in sent[0]
    reloaded = load_progress(ctx["view"])
    assert reloaded.decision_values == {"grouping": "mode"}
    assert pending_actions.get(ctx["prefix"]) is not None
    assert not pending_path.exists()


def test_missing_proc_grace_interval_and_durable_retry(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing proc rows wait out the grace interval; failures retry durably."""
    import sase_telegram.inbound_handlers.gate_completions as gc
    from sase_telegram.plan_decisions import STALE_TEXT

    ctx = _deferred_submit_context(gate_home, "telegram-deferred-missing", monkeypatch)
    bundle, record = ctx["bundle"], ctx["record"]
    monkeypatch.setattr(gc, "_gate_answer_proc_status", lambda proc_id: None)
    # Within the grace interval: still waiting, nothing sent.
    early_path = _write_completion_record(tmp_path, dict(record))
    with (
        patch.object(gc.telegram_client, "send_message") as sent,
        patch.object(gc.telegram_client, "edit_message_reply_markup") as edited,
    ):
        assert (
            gc._settle_decision_receipt(
                early_path,
                json.loads(early_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + 1,
            )
            is False
        )
        assert sent.call_count == 0 and edited.call_count == 0
    assert early_path.exists()
    # Past the interval: one missing-output report with usable controls.
    late_path = _write_completion_record(tmp_path, dict(record))
    sent_texts: list[str] = []
    with (
        patch.object(
            gc.telegram_client,
            "send_message",
            side_effect=lambda c, t: sent_texts.append(t),
        ),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert (
            gc._settle_decision_receipt(
                late_path,
                json.loads(late_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + gc.MISSING_PROC_GRACE_SECONDS + 1,
            )
            is True
        )
    assert len(sent_texts) == 1 and "retry from the review card" in sent_texts[0]
    assert load_progress(ctx["view"]).decision_values == {"grouping": "mode"}
    # Failed delivery keeps the durable retry instead of claiming recovery.
    retry_path = _write_completion_record(tmp_path, dict(record))
    errors = bundle / "errors"
    errors.mkdir(parents=True, exist_ok=True)
    (errors / "stale.json").write_text(
        json.dumps(
            {
                "code": "stale_review",
                "message": "stale_review: current revision is 1",
                "created_at_unix": ctx["created"] + 1,
            }
        ),
        encoding="utf-8",
    )
    with patch.object(
        gc.telegram_client, "send_message", side_effect=RuntimeError("net down")
    ):
        assert (
            gc._settle_decision_receipt(
                retry_path,
                json.loads(retry_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + 2,
            )
            is False
        )
    assert retry_path.exists()
    # A still-running visible proc never reports, even past the interval.
    running_path = _write_completion_record(tmp_path, dict(record))
    (errors / "stale.json").unlink(missing_ok=True)
    monkeypatch.setattr(
        gc,
        "_gate_answer_proc_status",
        lambda proc_id: SimpleNamespace(status="running"),
    )
    with (
        patch.object(gc.telegram_client, "send_message") as sent,
        patch.object(gc.telegram_client, "edit_message_reply_markup") as edited,
    ):
        assert (
            gc._settle_decision_receipt(
                running_path,
                json.loads(running_path.read_text(encoding="utf-8")),
                bundle,
                "chat-1",
                record["proc_id"],
                ctx["created"],
                now=ctx["created"] + gc.MISSING_PROC_GRACE_SECONDS + 999,
            )
            is False
        )
        assert sent.call_count == 0 and edited.call_count == 0
    assert STALE_TEXT  # exact stale wording is pinned above


def _submit_approve_commit(request_id: str, gate_home: Path) -> dict[str, Any]:
    """Submit approve+commit through real callbacks; return context."""
    from sase_telegram.inbound_handlers.callbacks import _handle_callback

    notification = _tale_notification(gate_home, request_id)
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
    return {
        "notification": notification,
        "prefix": prefix,
        "view": view,
        "revision": revision,
        "bundle": Path(notification.action_data["bundle_path"]),
    }


def test_native_telegram_settlement_edits_once(gate_home: Path) -> None:
    """Completion edits the card once, replies once, and never duplicates."""
    import sase_telegram.inbound_handlers.gate_completions as gc
    from sase.notifications import store as _store

    ctx = _submit_approve_commit("telegram-settle-native", gate_home)
    bundle, prefix = ctx["bundle"], ctx["prefix"]
    comp_dir = Path(inbound.GATE_COMPLETION_PENDING_DIR)
    pending_files = sorted(comp_dir.glob("*.json"))
    assert len(pending_files) == 1
    edits: list[str] = []
    replies: list[str] = []
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(gc, "_gate_answer_proc_status", return_value=None),
        patch.object(
            gc.telegram_client,
            "edit_message_text",
            side_effect=lambda c, m, t, reply_markup=None: edits.append(t),
        ),
        patch.object(
            gc.telegram_client,
            "send_message",
            side_effect=lambda c, t, **k: replies.append(t),
        ),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert gc._send_ready_gate_completions() == 1
    assert len(edits) == 1
    assert edits[0].splitlines()[0].startswith("✅ Tale approved · you via Telegram")
    assert "grouping → mode" in edits[0]
    assert len(replies) == 1 and replies[0] == edits[0]
    assert pending_actions.get(prefix) is None
    assert not (bundle / "telegram_gate_progress.json").exists()
    assert list(comp_dir.glob("*.json")) == []
    # Repeated polls and a receiver restart preserve these facts.
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(gc, "_gate_answer_proc_status", return_value=None),
        patch.object(gc.telegram_client, "edit_message_text") as edited,
        patch.object(gc.telegram_client, "send_message") as sent,
    ):
        assert gc._send_ready_gate_completions() == 0
        _store._LOAD_CACHE.clear()
        assert gc._send_ready_gate_completions() == 0
        assert edited.call_count == 0 and sent.call_count == 0
    # The post-poll sweep cannot perform a second settlement edit.
    from sase_telegram.inbound_handlers.keyboard_cleanup import (
        _settle_externally_resolved_decision,
    )

    with (
        patch("sase_telegram.inbound_handlers.keyboard_cleanup.telegram_client"),
    ):
        assert _settle_externally_resolved_decision(prefix, 42, "chat-1") is False


def test_external_settlement_surfaces_and_feedback_null_source(
    gate_home: Path,
) -> None:
    """ACE/CLI settlement renders truthfully; feedback edits the card w/o source."""
    import sase_telegram.inbound_handlers.gate_completions as gc

    cases = [
        ("telegram-settle-ace", {"caller": "human", "source": "tui"}, "you via ACE"),
        ("telegram-settle-cli", {"caller": "human", "source": "cli"}, "you via CLI"),
    ]
    for request_id, provenance, attribution in cases:
        ctx = _submit_approve_commit(request_id, gate_home)
        bundle, prefix = ctx["bundle"], ctx["prefix"]
        response_path = bundle / "response.json"
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response.update(provenance)
        response_path.write_text(json.dumps(response, indent=2), encoding="utf-8")
        comp_dir = Path(inbound.GATE_COMPLETION_PENDING_DIR)
        edits: list[str] = []
        with (
            patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
            patch.object(gc, "_gate_answer_proc_status", return_value=None),
            patch.object(
                gc.telegram_client,
                "edit_message_text",
                side_effect=lambda c, m, t, reply_markup=None, _edits=edits: (
                    _edits.append(t)
                ),
            ),
            patch.object(gc.telegram_client, "send_message"),
            patch.object(gc.telegram_client, "edit_message_reply_markup"),
        ):
            assert gc._send_ready_gate_completions() == 1
        assert (
            edits[0].splitlines()[0].startswith(f"✅ Tale approved · {attribution}")
        ), edits[0].splitlines()[0]
        assert pending_actions.get(prefix) is None
    # External reject never renders as approved.
    ctx = _submit_approve_commit("telegram-settle-reject-src", gate_home)
    bundle = ctx["bundle"]
    response_path = bundle / "response.json"
    response = json.loads(response_path.read_text(encoding="utf-8"))
    response["selected_option_ids"] = ["reject"]
    response["caller"] = "human"
    response["source"] = "cli"
    response_path.write_text(json.dumps(response, indent=2), encoding="utf-8")
    comp_dir = Path(inbound.GATE_COMPLETION_PENDING_DIR)
    edits = []
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(gc, "_gate_answer_proc_status", return_value=None),
        patch.object(
            gc.telegram_client,
            "edit_message_text",
            side_effect=lambda c, m, t, reply_markup=None: edits.append(t),
        ),
        patch.object(gc.telegram_client, "send_message"),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert gc._send_ready_gate_completions() == 1
    assert edits[0].splitlines()[0].startswith("❌ Rejected · you via CLI")
    assert "approved" not in edits[0].splitlines()[0].lower().replace("rejected", "")
    # Feedback with a null source id still edits the original card.
    from dataclasses import replace

    from sase_telegram.gate_flow import save_progress as save_gate_progress
    from sase_telegram.inbound_handlers.callbacks import _handle_callback
    from sase_telegram.inbound_handlers.text_messages import _handle_text_message

    notification = _tale_notification(gate_home, "telegram-settle-feedback")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    view = load_gate_view(notification.action_data, expected_kind="plan")
    revision = view.review_revision
    feedback_branch = next(
        i for i, branch in enumerate(view.branches) if branch == ("feedback",)
    )
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
        _handle_callback(
            _callback(f"gate:{prefix}:f{feedback_branch}r{revision}"),
            {prefix: action},
        )
        message = SimpleNamespace(
            text="please split the difference",
            entities=None,
            message_id=100,
            reply_to_message=SimpleNamespace(message_id=42),
            chat=SimpleNamespace(id="chat-1"),
        )
        _handle_text_message(message, {})
    # Submission kept the card context; only the awaiting entry cleared.
    assert pending_actions.get(prefix) is not None
    bundle = Path(notification.action_data["bundle_path"])
    assert (bundle / "telegram_gate_progress.json").exists()
    assert (bundle / "response.json").exists()
    progress = load_progress(view)
    progress = replace(progress, source_message_id=None)
    save_gate_progress(view, progress)
    comp_dir = Path(inbound.GATE_COMPLETION_PENDING_DIR)
    edited_ids: list[int] = []
    fb_texts: list[str] = []
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(gc, "_gate_answer_proc_status", return_value=None),
        patch.object(
            gc.telegram_client,
            "edit_message_text",
            side_effect=lambda c, m, t, reply_markup=None: (
                edited_ids.append(m),
                fb_texts.append(t),
            ),
        ),
        patch.object(
            gc.telegram_client, "send_message", side_effect=lambda c, t, **k: None
        ),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert gc._send_ready_gate_completions() == 1
    assert edited_ids and edited_ids[0] == 42
    assert fb_texts[0].splitlines()[0].startswith("💬 Feedback sent · you via Telegram")
    assert "provisional" in fb_texts[0]
    assert pending_actions.get(prefix) is None


def test_launch_failure_claim_only_for_recorded_coder_failure(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structured coder-start failures earn the claim; nothing else does."""
    import sase_telegram.inbound_handlers.gate_completions as gc
    from sase_telegram.decision_receipt import launch_failed_text

    # Unit-level: only a recorded staged failure on a coder launch counts.
    approve = {"selected_option_ids": ["approve", "commit"]}
    running = SimpleNamespace(status="running", log_path=None)
    terminal = SimpleNamespace(status="success", log_path=None)
    staged = {"code": "coder_launch_failed", "message": "boom", "stage": "side_effects"}
    wordy = {"code": "other", "message": "coder could not start the thing"}
    legacy = {"code": "successor_launch_failed", "message": "x"}
    generic = {"code": "other", "message": "boom", "stage": "command"}
    assert (
        gc._is_launch_failure(staged, terminal, approve, bundle_path=tmp_path) is True
    )
    assert (
        gc._is_launch_failure(staged, running, approve, bundle_path=tmp_path) is False
    )
    assert (
        gc._is_launch_failure(wordy, terminal, approve, bundle_path=tmp_path) is False
    )
    assert (
        gc._is_launch_failure(legacy, terminal, approve, bundle_path=tmp_path) is False
    )
    assert (
        gc._is_launch_failure(generic, terminal, approve, bundle_path=tmp_path) is False
    )
    assert (
        gc._is_launch_failure(
            staged, terminal, {"selected_option_ids": ["commit"]}, bundle_path=tmp_path
        )
        is False
    )
    assert (
        gc._is_launch_failure(
            staged, terminal, {"selected_option_ids": ["reject"]}, bundle_path=tmp_path
        )
        is False
    )
    assert (
        gc._is_launch_failure(
            staged,
            terminal,
            {"selected_option_ids": ["feedback"]},
            bundle_path=tmp_path,
        )
        is False
    )
    # End to end: failure after first acceptance updates the same card once.
    ctx = _submit_approve_commit("telegram-launch-failure", gate_home)
    bundle = ctx["bundle"]
    before = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    journal = bundle / "journal.jsonl"
    journal.write_text(
        json.dumps(
            {"attempt_id": "a", "event": "stage_started", "stage": "side_effects"}
        )
        + "\n",
        encoding="utf-8",
    )
    comp_dir = Path(inbound.GATE_COMPLETION_PENDING_DIR)
    monkeypatch.setattr(
        gc,
        "_gate_answer_proc_status",
        lambda proc_id: SimpleNamespace(status="success"),
    )
    first_edits: list[str] = []
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(
            gc.telegram_client,
            "edit_message_text",
            side_effect=lambda c, m, t, reply_markup=None: first_edits.append(t),
        ),
        patch.object(gc.telegram_client, "send_message"),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        # Launch still observed: card settles but the job is retained.
        assert gc._send_ready_gate_completions() == 0
    assert len(first_edits) == 1 and "coder could not start" not in first_edits[0]
    assert sorted(comp_dir.glob("*.json")) != []
    errors = bundle / "errors"
    errors.mkdir(parents=True, exist_ok=True)
    (errors / "launch.json").write_text(
        json.dumps(
            {
                "code": "coder_launch_failed",
                "message": "coder launch failed",
                "stage": "side_effects",
                "created_at_unix": 9999999999.0,
            }
        ),
        encoding="utf-8",
    )
    follow_edits: list[str] = []
    with (
        patch.object(gc, "GATE_COMPLETION_PENDING_DIR", comp_dir),
        patch.object(
            gc.telegram_client,
            "edit_message_text",
            side_effect=lambda c, m, t, reply_markup=None: follow_edits.append(t),
        ),
        patch.object(gc.telegram_client, "send_message"),
        patch.object(gc.telegram_client, "edit_message_reply_markup"),
    ):
        assert gc._send_ready_gate_completions() == 1
    assert len(follow_edits) == 1
    assert "coder could not start" in follow_edits[0]
    # Accepted choices stay immutable across the follow-up edit.
    assert "grouping → mode" in follow_edits[0]
    after = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    assert after["option_inputs"] == before["option_inputs"]
    assert launch_failed_text("base").endswith("coder could not start · retry")


def test_keyboard_exact_rows_tokens_and_style(gate_home: Path) -> None:
    """Pin decision keyboard rows, tokens, success style, and byte budget."""
    from sase_telegram import callback_data as _cb
    from sase_telegram import decision_keyboard as _dk
    from sase_telegram.decision_callbacks import apply_decision_token

    notification = _tale_notification(gate_home, "telegram-keyboard-exact")
    view = load_gate_view(notification.action_data, expected_kind="plan")
    progress = load_progress(view)
    revision = view.review_revision
    keyboard = render_gate_keyboard(notification.id[:8], view, progress)
    assert keyboard is not None
    rows = [[b.text for b in row] for row in keyboard.inline_keyboard]
    assert rows[0] == ["◉ grouping: pane ▾"]
    assert rows[1] == ["☑️ terse"]
    assert any(text.startswith("✅ Tale · defaults") for row in rows for text in row)
    assert not any("Reset" in text for row in rows for text in row)
    # Changed draft marks the row and offers Reset.
    changed, _, _ = apply_decision_token(view, progress, f"d0=k1r{revision}")
    keyboard = render_gate_keyboard(notification.id[:8], view, changed)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "◉ grouping: mode ● ▾" in texts
    assert "↺ Reset" in texts
    # Choice sub-keyboard: radio marks, star default, changed dot, back row.
    opened, toast, _ = apply_decision_token(view, progress, f"d0>r{revision}")
    assert "How should the overlay group bindings?" in toast
    keyboard = render_gate_keyboard(notification.id[:8], view, opened)
    sub = [[b.text for b in row] for row in keyboard.inline_keyboard]
    assert sub[0] == ["◉ pane ★"]
    assert sub[1] == ["○ mode"]
    assert sub[-1] == ["↩ Back"]
    # Every callback payload fits the 64-byte Telegram budget.
    for button in [b for row in keyboard.inline_keyboard for b in row]:
        assert len(button.callback_data.encode("utf-8")) <= 64
    # Memory card rows carry the brain chip; epic cards name Epic.
    memory = _tale_notification(
        gate_home, "telegram-keyboard-memory", MEMORY_DECISIONS_PLAN
    )
    memory_view = load_gate_view(memory.action_data, expected_kind="plan")
    memory_keyboard = render_gate_keyboard(
        memory.id[:8], memory_view, load_progress(memory_view)
    )
    assert memory_keyboard is not None
    memory_texts = [b.text for row in memory_keyboard.inline_keyboard for b in row]
    assert any("🧠" in text and "tui_note" in text for text in memory_texts)
    epic = _tale_notification(gate_home, "telegram-keyboard-epic", EPIC_DECISIONS_PLAN)
    epic_view = load_gate_view(epic.action_data)
    epic_keyboard = render_gate_keyboard(
        epic.id[:8], epic_view, load_progress(epic_view)
    )
    assert epic_keyboard is not None
    epic_texts = [b.text for row in epic_keyboard.inline_keyboard for b in row]
    assert any(text.startswith("✅ Epic") for text in epic_texts)
    # Primary styling: success where supported, compatible fallback otherwise.
    primary = _dk.primary_button("✅ Tale · defaults", "c0r1", "prefix01")
    if _dk._supports_style():
        assert getattr(primary, "style", None) == "success"
    else:
        assert primary.text.startswith("✅ Tale")
    decoded = _cb.decode(primary.callback_data)
    assert decoded.choice == "c0r1"
    # Token helpers stay within budget and parse back.
    from sase_telegram.plan_decisions import (
        encode_back_token,
        encode_open_token,
        encode_refresh_token,
        encode_reset_token,
        encode_set_token,
        parse_decision_token,
    )

    for token in (
        encode_set_token(0, "k1", revision),
        encode_open_token(0, revision),
        encode_back_token(revision),
        encode_reset_token(revision),
        encode_refresh_token(revision),
    ):
        assert parse_decision_token(token) is not None
        assert len(_cb.encode("gate", notification.id[:8], token).encode()) <= 64


def test_sheet_three_degradations_only(gate_home: Path) -> None:
    """Step-by-step budget fixtures prove the exact three degradations."""
    from sase_telegram.decision_sheet import (
        _render_sheet_text,
        render_decision_sheet,
    )
    from sase_telegram.formatting import escape_markdown_v2

    memory_definition = {
        "id": "tui_note",
        "kind": "toggle",
        "ask": "Record the overlay conventions in the tui memory note?",
        "why": None,
        "choices": [],
        "default": True,
        "memory": {
            "selectors": ["tui.md"],
            "resolved": [
                {
                    "selector": "tui.md",
                    "type": "reference",
                    "scope": "project",
                    "exists": False,
                }
            ],
            "provenance": "asked",
            "quote": "and note the convention in the tui memory",
        },
    }
    choice_definition = {
        "id": "grouping",
        "kind": "choice",
        "ask": "How should the overlay group bindings?",
        "why": "pane keeps the footer's order",
        "choices": [
            {"key": "pane", "label": "By pane, matching the footer hints"},
            {"key": "mode", "label": "By leader mode, denser <&>"},
        ],
        "default": "pane",
    }
    frozen = [choice_definition, memory_definition]
    full = _render_sheet_text(frozen, {})
    assert "By pane, matching the footer hints" in full
    assert "By leader mode, denser <&>" in full
    stage1 = _render_sheet_text(frozen, {}, drop_non_default_labels=True)
    assert "By pane, matching the footer hints" in stage1
    assert "By leader mode, denser" not in stage1
    assert "mode" in stage1
    stage2 = _render_sheet_text(frozen, {}, drop_all_labels=True)
    assert "By pane" not in stage2 and "By leader mode" not in stage2
    for stage in (full, stage1, stage2):
        # Starred defaults, why, memory, new chip, and quotes survive all.
        assert "★ pane" in stage
        assert "pane keeps the footer's order" in stage
        assert "tui.md" in stage and "you asked" in stage
        assert "new" in stage
        assert "and note the convention in the tui memory" in stage
    # The expandable stage quotes only choice lines; asks/memory stay out.
    huge = render_decision_sheet(frozen, None, 1, budget=10)
    assert "||" in huge
    head, _, quoted = huge.partition("**>")
    assert "How should the overlay group bindings?" in head
    assert "tui\\.md" in head
    assert "and note the convention" in head
    # Only choice lines are quoted: asks and memory stay outside.
    assert "How should the overlay group bindings?" not in quoted
    assert "tui\\.md" not in quoted
    assert "and note the convention" not in quoted
    assert "pane" in quoted
    assert not huge.rstrip().endswith("\\")
    # Escapes are complete on every stage, and a formatted card fits 4096.
    for candidate in (full, stage1, stage2):
        escaped = escape_markdown_v2(candidate)
        assert not escaped.rstrip().endswith("\\")
    notification = _tale_notification(gate_home, "telegram-sheet-card")
    text, _, _ = format_notification(notification)
    assert len(text) <= 4096
    assert "Decisions · 2" in text


def test_pdf_accepted_values_callouts_and_cleanup(
    gate_home: Path, tmp_path: Path
) -> None:
    """Accepted-plan PDF carries frozen values, callouts, and cleans up."""
    from sase_telegram.decision_pdf import _label_callouts, preprocess_plan_for_pdf

    ctx = _submit_approve_commit("telegram-pdf-accepted", gate_home)
    bundle = ctx["bundle"]
    pending_src = bundle / "plan.md"
    out = preprocess_plan_for_pdf(
        pending_src, gate_context={"bundle_path": str(bundle)}
    )
    assert out is not None
    content = out.read_text(encoding="utf-8")
    # Non-default accepted values and correct defaults render in the table.
    assert "## Decisions" in content
    assert "mode" in content
    assert "decisions:" not in content
    out.unlink(missing_ok=True)
    # Labelled callout continuations keep every line and the source is kept.
    plan = tmp_path / "plan.md"
    plan.write_text(TALE_DECISIONS_PLAN, encoding="utf-8")
    body = "> [!decision] grouping = pane First\n> continuation line\n\ntext"
    labelled = _label_callouts(body)
    assert "Decision" in labelled and "continuation" in labelled
    assert plan.read_text(encoding="utf-8") == TALE_DECISIONS_PLAN
    # The new memory chip renders from a temporary fixture, never canonical.
    project = tmp_path / "memory-pdf"
    mem_dir = project / "sase" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "glossary").mkdir(exist_ok=True)
    notification = _tale_notification(
        gate_home, "telegram-pdf-memory", MEMORY_DECISIONS_PLAN
    )
    text, _, _ = format_notification(notification)
    assert "🧠" in text


def test_epic_primary_action_and_verdict(gate_home: Path) -> None:
    """Epic cards carry the Epic primary action and epic-specific verdict."""
    from sase_telegram import callback_data as _cb
    from sase_telegram.decision_receipt import approval_verdict, receipt_text

    notification = _tale_notification(
        gate_home, "telegram-epic-card", EPIC_DECISIONS_PLAN
    )
    notification.action = "EpicApproval"
    text, keyboard, _ = format_notification(notification)
    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any(t.startswith("✅ Epic") for t in texts)
    assert "grouping" in text
    view = load_gate_view(notification.action_data)
    assert approval_verdict(view, {"selected_option_ids": ["approve"]}) == "epic launch"
    receipt = receipt_text(
        view,
        {"grouping": "mode"},
        response={
            "selected_option_ids": ["approve"],
            "caller": "reviewer",
            "source": "telegram",
            "responded_at_unix": 1750000000,
        },
    )
    assert receipt.splitlines()[0].startswith("✅ Epic approved · you via Telegram")
    assert "grouping → mode" in receipt
    for button in [b for row in keyboard.inline_keyboard for b in row]:
        assert len(button.callback_data.encode("utf-8")) <= 64
        _cb.decode(button.callback_data)
