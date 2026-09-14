"""Shared pytest environment isolation for the Telegram plugin."""

from collections.abc import Iterator
import os

import pytest
from sase.env_contracts import WORKSPACE_PIN_ENV_VARS
from sase.feature_flags import SASE_FEATURE_FLAGS_ENV


SASE_MODEL_ALIAS_OVERRIDES_ENV = "SASE_MODEL_ALIAS_OVERRIDES"


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
