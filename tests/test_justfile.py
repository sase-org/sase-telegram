from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "Justfile"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
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


def test_install_dry_run_routes_sase_through_overrides(tmp_path: Path) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    source = repo / ".sase-deps" / "sase"
    _make_sase_source(source)
    _make_sase_core_source(repo / ".sase-deps" / "sase-core")

    result = _run_just(repo, "--dry-run", "install")
    output = result.stdout + result.stderr

    overrides_file = repo / ".sase-overrides.txt"
    project_install = f"uv pip install --python '.venv/bin/python' --overrides '{overrides_file}' -e \".[dev]\""
    core_install = "just _install-local-sase-core"
    assert "printf -- '-e %s" in output
    assert f"'{source}'" in output
    assert str(overrides_file) in output
    assert project_install in output
    assert core_install in output
    assert output.index("printf -- '-e %s") < output.index(project_install)
    assert output.index(project_install) < output.index(core_install)
    assert f"--no-deps -e '{source}'" not in output


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

    overrides_file = repo / ".sase-overrides.txt"
    assert "printf -- '-e %s" in output
    assert f"'{source}'" in output
    assert str(overrides_file) in output
    assert (
        f"uv pip install --python '.venv/bin/python' --overrides '{overrides_file}' -e \".[dev]\""
    ) in output
    assert "just _install-local-sase-core" in output
    assert f"--no-deps -e '{source}'" not in output


def test_write_sase_overrides_writes_editable_source(tmp_path: Path) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    _make_sase_source(repo / ".sase-deps" / "sase")

    selected = _selected_sase_source(repo)
    _run_just(repo, "_write-sase-overrides")

    assert (repo / ".sase-overrides.txt").read_text() == f"-e {selected}\n"


def test_install_source_sase_dry_run_installs_sase_and_builds_core(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "sase-telegram"
    _copy_justfile(repo)
    source = repo / ".sase-deps" / "sase"
    _make_sase_source(source)
    _make_sase_core_source(repo / ".sase-deps" / "sase-core")

    result = _run_just(repo, "--dry-run", "install-source-sase", "/tmp/x/bin/python")
    output = result.stdout + result.stderr

    assert (
        f"uv pip install --python \"/tmp/x/bin/python\" --no-deps -e '{source}'"
        in output
    )
    assert 'uv pip install --python "/tmp/x/bin/python" maturin' in output
    assert "PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1" in output
    assert 'maturin" develop --release' in output


def test_publish_install_smoke_uses_overrides_and_install_source_sase() -> None:
    workflow = PUBLISH_WORKFLOW.read_text()
    smoke_job = workflow.split("  install-smoke:\n", maxsplit=1)[1].split(
        "  publish:\n", maxsplit=1
    )[0]

    assert "repository: sase-org/sase\n" in smoke_job
    assert "repository: sase-org/sase-core\n" in smoke_job
    assert "--overrides /tmp/sase-overrides.txt dist/*.whl" in smoke_job
    assert "just install-source-sase /tmp/smoke-venv/bin/python" in smoke_job
    assert smoke_job.index("dist/*.whl") < smoke_job.index("install-source-sase")


def test_ci_pins_authenticated_just_setup() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    steps = workflow["jobs"]["check"]["steps"]
    setup_just = next(
        step for step in steps if step.get("uses") == "extractions/setup-just@v2"
    )

    assert setup_just["with"] == {
        "just-version": "1.58.0",
        "github-token": "${{ secrets.SASE_RELEASE_TOKEN || github.token }}",
    }
