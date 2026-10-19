from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/release-dependency-floor.yml"
REUSABLE = "uses: sase-org/sase/.github/workflows/release-dependency-floor.yml@master"


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_floor_check_caller_triggers_and_never_skips() -> None:
    workflow = _read(WORKFLOW)

    assert REUSABLE in workflow
    assert "pull_request:" in workflow
    assert "branches: [master]" in workflow
    assert "schedule:" in workflow
    assert re.search(r"cron: '\d+ \* \* \* \*'", workflow)
    assert "workflow_dispatch:" in workflow
    assert "probe_requirement:" in workflow
    assert "actions: write" in workflow
    assert "if:" not in workflow
    assert "  release-dependency-floor:" in workflow
