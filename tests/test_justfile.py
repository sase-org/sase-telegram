from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "Justfile"
LOCAL_SASE_ENV = "SASE_TELEGRAM_SASE_SOURCE_DIR"
LOCAL_SASE_CORE_ENV = "SASE_TELEGRAM_SASE_CORE_SOURCE_DIR"


def _copy_justfile(repo: Path) -> None:
    repo.mkdir(parents=True)
    shutil.copyfile(JUSTFILE, repo / "Justfile")


def _make_sase_source(path: Path) -> None:
    (path / "src" / "sase").mkdir(parents=True)
    (path / "pyproject.toml").write_text('[project]\nname = "sase"\n')


def _make_sase_core_source(path: Path) -> None:
    pyproject = path / "crates" / "sase_core_py" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text('[project]\nname = "sase-core-rs"\n')


def _run_just(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.pop(LOCAL_SASE_ENV, None)
    run_env.pop(LOCAL_SASE_CORE_ENV, None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [
            "just",
            "--no-dotenv",
            "--justfile",
            str(repo / "Justfile"),
            "--working-directory",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        env=run_env,
        text=True,
    )


def _selected_sase_source(repo: Path, env: dict[str, str] | None = None) -> Path:
    result = _run_just(repo, "_local-sase-source", env=env)
    return Path(result.stdout.strip()).resolve()


def _selected_sase_core_source(repo: Path, env: dict[str, str] | None = None) -> Path:
    result = _run_just(repo, "_local-sase-core-source", env=env)
    return Path(result.stdout.strip()).resolve()


def test_local_sase_source_override_wins_over_default_candidates(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    _make_sase_source(repo / ".sase-deps" / "sase")
    override = tmp_path / "override-sase"
    _make_sase_source(override)

    selected = _selected_sase_source(repo, {LOCAL_SASE_ENV: str(override)})

    assert selected == override.resolve()


def test_ci_dependency_checkout_is_used_when_local_sase_checkouts_are_absent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    ci_source = repo / ".sase-deps" / "sase"
    _make_sase_source(ci_source)

    selected = _selected_sase_source(repo)

    assert selected == ci_source.resolve()


def test_local_sase_core_source_override_wins_over_default_candidates(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    _make_sase_core_source(repo / ".sase-deps" / "sase-core")
    override = tmp_path / "override-sase-core"
    _make_sase_core_source(override)

    selected = _selected_sase_core_source(repo, {LOCAL_SASE_CORE_ENV: str(override)})

    assert selected == override.resolve()


def test_sibling_sase_core_checkout_is_the_default_for_local_development(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dev"
    repo = root / "sase-telegram"
    _copy_justfile(repo)
    core_source = root / "sase-core"
    _make_sase_core_source(core_source)

    selected = _selected_sase_core_source(repo)

    assert selected == core_source.resolve()


def test_ci_dependency_sase_core_checkout_is_used_when_sibling_is_absent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    core_source = repo / ".sase-deps" / "sase-core"
    _make_sase_core_source(core_source)

    selected = _selected_sase_core_source(repo)

    assert selected == core_source.resolve()


def test_sibling_checkout_is_used_when_ci_checkout_is_absent(tmp_path: Path) -> None:
    root = tmp_path / "dev"
    repo = root / "sase-telegram"
    _copy_justfile(repo)
    sibling_source = root / "sase"
    _make_sase_source(sibling_source)

    selected = _selected_sase_source(repo)

    assert selected == sibling_source.resolve()


def test_linked_workspace_checkout_is_the_final_default(tmp_path: Path) -> None:
    workspace = tmp_path / "sase_42"
    repo = workspace / "sase" / "repos" / "linked" / "sase-telegram"
    _copy_justfile(repo)
    _make_sase_source(workspace)

    selected = _selected_sase_source(repo)

    assert selected == workspace.resolve()


def test_linked_workspace_checkout_wins_over_ci_dependency_checkout(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "sase_42"
    repo = workspace / "sase" / "repos" / "linked" / "sase-telegram"
    _copy_justfile(repo)
    _make_sase_source(workspace)
    _make_sase_source(repo / ".sase-deps" / "sase")

    selected = _selected_sase_source(repo)

    assert selected == workspace.resolve()


def test_install_dry_run_installs_project_before_local_sase(tmp_path: Path) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    source = repo / ".sase-deps" / "sase"
    _make_sase_source(source)
    _make_sase_core_source(repo / ".sase-deps" / "sase-core")

    result = _run_just(repo, "--dry-run", "install")
    output = result.stdout + result.stderr

    project_install = "uv pip install --python '.venv/bin/python' -e \".[dev]\""
    core_install = "just _install-local-sase-core"
    local_install = (
        f"uv pip install --python '.venv/bin/python' --no-deps -e '{source}'"
    )
    assert project_install in output
    assert core_install in output
    assert local_install in output
    assert output.index(project_install) < output.index(core_install)
    assert output.index(core_install) < output.index(local_install)


def test_local_sase_core_install_dry_run_uses_maturin_develop(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    core_source = repo / ".sase-deps" / "sase-core"
    _make_sase_core_source(core_source)

    result = _run_just(repo, "--dry-run", "_install-local-sase-core")
    output = result.stdout + result.stderr

    assert "uv pip install --python '.venv/bin/python' maturin" in output
    assert f"cd '{core_source / 'crates' / 'sase_core_py'}'" in output
    assert "PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1" in output
    assert "maturin' develop --release" in output


def test_setup_dry_run_overlays_local_sase(tmp_path: Path) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    source = repo / ".sase-deps" / "sase"
    _make_sase_source(source)
    _make_sase_core_source(repo / ".sase-deps" / "sase-core")

    result = _run_just(repo, "--dry-run", "_setup")
    output = result.stdout + result.stderr

    assert "just _install-local-sase-core" in output
    assert (
        f"uv pip install --python '.venv/bin/python' --no-deps -e '{source}'"
    ) in output
