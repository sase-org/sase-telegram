"""Shared pytest environment isolation for the Telegram plugin."""

from collections.abc import Iterator
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sase.env_contracts import WORKSPACE_PIN_ENV_VARS
from sase.feature_flags import SASE_FEATURE_FLAGS_ENV


SASE_MODEL_ALIAS_OVERRIDES_ENV = "SASE_MODEL_ALIAS_OVERRIDES"

if TYPE_CHECKING:
    from sase.project_tags.catalog import ProjectTagCatalog


@pytest.fixture()
def _fake_tag_catalog() -> "Iterator[ProjectTagCatalog]":
    """Warm the tag-catalog seams with a fake single-project catalog.

    Routes catalog loads at a fake ``sase`` target (real core bindings,
    so tag grammar and casefold resolution are genuine), warms the peek
    snapshot the display humanizer reads, and provides a non-empty display
    map so the humanizer reaches tagify. Uses the ``git`` workflow because
    that is the workspace provider sase core ships itself. Everything is
    restored afterwards so the cold-catalog ``#`` assertions elsewhere keep
    passing. The display-map seam lives in
    ``sase._project_display_labels`` (not the public
    ``sase.project_display_names`` facade) because the humanizers resolve
    ``cached_project_display_snapshot`` from that module's namespace.
    """
    import sase._project_display_labels as display_names_module
    import sase.project_tags.catalog as tag_catalog_module
    from sase.project_tags.catalog import ProjectTagCatalog, ProjectTagTarget

    fake = ProjectTagCatalog(
        targets=(
            ProjectTagTarget(
                key="sase",
                name="sase",
                tag="+sase",
                workflow_type="git",
                vcs_ref="#git:sase",
                provider_display="Git (bare)",
                state="enabled",
            ),
        ),
        accent_palette=(),
    )
    with (
        patch.object(tag_catalog_module, "load_project_tag_catalog", return_value=fake),
        patch.object(tag_catalog_module, "peek_project_tag_catalog", return_value=fake),
        patch.object(
            display_names_module,
            "cached_project_display_snapshot",
            return_value={"sase": "sase"},
        ),
    ):
        yield fake


@pytest.fixture(autouse=True)
def _clear_agent_env_vars(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear ambient SASE agent state before every test.

    SASE agents launch subprocesses with env vars that identify the live
    agent, artifact store, feature flags, and proc/gate machinery. If pytest
    inherits those values, tests that create gates can register real gate
    shells or notifications against the host project instead of the test
    sandbox, and feature-flag overrides can change assertions unexpectedly.
    """
    keys_to_clear = {
        key
        for key in os.environ
        if (
            key.startswith("SASE_AGENT_")
            or key.startswith("SASE_LINKED_REPO_")
            or key.startswith("SASE_MONITOR_DELIVERY_")
            or key.startswith("SASE_PROC_")
            or key.startswith("SASE_SIBLING_REPO_")
            or key
            in {
                "SASE_AGENT",
                "SASE_ARTIFACTS_DIR",
                "SASE_BEAD_ID",
                "SASE_CHOP_LUMBERJACK",
                "SASE_CHOP_NAME",
                "SASE_CHOP_PROMPT_HASH",
                "SASE_CHOP_RUN_ID",
                "SASE_JOB_NAME",
                "SASE_JOB_PROMPT_HASH",
                "SASE_JOB_ROUTINE",
                "SASE_JOB_RUN_ID",
                SASE_FEATURE_FLAGS_ENV,
                "SASE_LINKED_REPOS_JSON",
                "SASE_MONITOR_CONTINUATION",
                "SASE_SIBLING_REPOS_JSON",
                SASE_MODEL_ALIAS_OVERRIDES_ENV,
                "TMUX_PANE",
            }
        )
    }
    keys_to_clear.update(WORKSPACE_PIN_ENV_VARS)

    for key in keys_to_clear:
        monkeypatch.delenv(key, raising=False)

    yield

    leaked_proc_keys = [key for key in os.environ if key.startswith("SASE_PROC_")]
    for key in (
        *WORKSPACE_PIN_ENV_VARS,
        SASE_FEATURE_FLAGS_ENV,
        SASE_MODEL_ALIAS_OVERRIDES_ENV,
        *leaked_proc_keys,
    ):
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _no_service_owned_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host machine's service config from claiming the receiver.

    ``ensure_receiver_running`` defers to a configured ``telegram_receiver``
    service proc; without this stub, tests would depend on whether the machine
    running them has adopted that proc. Tests that exercise the service-owned
    path override ``load_service_config`` themselves.
    """
    monkeypatch.setattr(
        "sase.service.config.load_service_config",
        lambda: SimpleNamespace(get=lambda _name: None),
    )


@pytest.fixture(autouse=True)
def _isolate_telegram_registration_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests off the real ~/.sase/telegram registration cache and lock.

    Integration tests that call ``inbound_main(["--once"])`` with a mocked
    telegram client would otherwise write the real
    ``~/.sase/telegram/commands_registered_ts`` and lock file. Tests that
    patch ``_COMMANDS_REGISTERED_PATH`` themselves keep working because
    their patch applies on top of this one.
    """
    import sase_telegram.inbound_handlers.commands as _commands
    import sase_telegram.scripts.sase_tg_inbound as _tg_inbound

    monkeypatch.setattr(
        _commands,
        "_COMMANDS_REGISTERED_PATH",
        tmp_path / "commands_registered_ts",
    )
    monkeypatch.setattr(
        _tg_inbound,
        "_HOUSEKEEPING_LOCK_FILE",
        tmp_path / "inbound_housekeeping.lock",
    )


@pytest.fixture(autouse=True)
def _isolate_partial_delivery_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep outbound runs off the real ~/.sase/telegram partial-delivery record.

    Every successful outbound send clears that record and every partial send
    writes it, so an unpatched suite would delete or forge the live machine's
    resume state.
    """
    import sase_telegram.outbound as _outbound

    monkeypatch.setattr(
        _outbound,
        "PARTIAL_DELIVERY_FILE",
        tmp_path / "partial_delivery.json",
    )


@pytest.fixture(autouse=True)
def _isolate_git_remote_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the network: no test may run ``ssh -T git@github.com``.

    sase's plan-gate preflight (``preflight_plan_archive_credential``) asks the
    git remote whether this host's credential works before accepting a plan
    approval that selects ``commit``. On CI runners without an SSH key that
    answers ``denied`` and refuses the answer, while developer machines answer
    ``ok``. Consumers resolve ``probe_git_remote_auth`` through its module at
    call time, so answering ``unknown`` (never a refusal) here makes every test
    hermetic.
    """
    monkeypatch.setattr(
        "sase.service.ssh_agent.probe_git_remote_auth", lambda _env: "unknown"
    )


@pytest.fixture(autouse=True)
def degraded_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, ...]]:
    """Keep degraded-state tracking off the live machine.

    Every failing outbound send or receiver poll records a degraded state
    under ``~/.sase/telegram`` and raises or settles a SASE notification, and
    every failed token lookup writes the negative-cache file. Redirect the
    state files into ``tmp_path`` and record notice calls instead of writing
    the real notification store. Tests that exercise the store-backed notice
    path patch the store functions themselves.
    """
    import sase_telegram.credentials as _credentials
    import sase_telegram.degraded as _degraded

    monkeypatch.setattr(_degraded, "STATE_DIR", tmp_path / "telegram-degraded")
    monkeypatch.setattr(
        _credentials,
        "bot_token_negative_cache_path",
        lambda: tmp_path / "telegram-degraded" / "bot_token_unavailable.json",
    )
    calls: list[tuple[str, ...]] = []

    def _raise(component: str, _label: str, record: object) -> str:
        notice_id = f"notice-{len(calls)}"
        calls.append(("raise", component, getattr(record, "state", ""), notice_id))
        return notice_id

    def _settle(notice_id: str, note: str) -> None:
        calls.append(("settle", notice_id, note))

    monkeypatch.setattr(_degraded, "_raise_notice", _raise)
    monkeypatch.setattr(_degraded, "_settle_notice", _settle)
    return calls
