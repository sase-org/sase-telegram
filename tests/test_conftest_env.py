"""Canary tests for shared pytest environment isolation."""

import os

from sase.feature_flags import SASE_FEATURE_FLAGS_ENV


def test_agent_environment_is_scrubbed() -> None:
    assert "SASE_AGENT" not in os.environ
    assert SASE_FEATURE_FLAGS_ENV not in os.environ
