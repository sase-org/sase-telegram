# sase-telegram task runner

repo_dir := justfile_directory()
venv_dir := ".venv"
venv_path := clean(repo_dir / venv_dir)
venv_bin := venv_dir / "bin"
venv_python := venv_bin / "python"
venv_maturin := clean(repo_dir / venv_bin / "maturin")
sase_overrides_file := clean(repo_dir / ".sase-overrides.txt")

# Override with SASE_TELEGRAM_SASE_SOURCE_DIR=/path/to/sase when the local
# checkout is not in one of the standard development locations below.
local_sase_source_override := env_var_or_default("SASE_TELEGRAM_SASE_SOURCE_DIR", "")
ci_sase_source := clean(repo_dir / ".sase-deps" / "sase")
sibling_sase_source := clean(repo_dir / ".." / "sase")
linked_workspace_sase_source := clean(repo_dir / ".." / ".." / ".." / "..")
local_sase_source := if local_sase_source_override != "" { clean(local_sase_source_override) } else if path_exists(linked_workspace_sase_source / "src" / "sase") == "true" { linked_workspace_sase_source } else if path_exists(sibling_sase_source / "src" / "sase") == "true" { sibling_sase_source } else { ci_sase_source }

# Override with SASE_TELEGRAM_SASE_CORE_SOURCE_DIR=/path/to/sase-core when the
# matching Rust core checkout is not next to this repo or in CI dependencies.
local_sase_core_source_override := env_var_or_default("SASE_TELEGRAM_SASE_CORE_SOURCE_DIR", "")
ci_sase_core_source := clean(repo_dir / ".sase-deps" / "sase-core")
sibling_sase_core_source := clean(repo_dir / ".." / "sase-core")
local_sase_core_source := if local_sase_core_source_override != "" { clean(local_sase_core_source_override) } else if path_exists(sibling_sase_core_source / "crates" / "sase_core_py" / "pyproject.toml") == "true" { sibling_sase_core_source } else if path_exists(ci_sase_core_source / "crates" / "sase_core_py" / "pyproject.toml") == "true" { ci_sase_core_source } else { "" }
local_sase_core_py_source := local_sase_core_source / "crates" / "sase_core_py"

default:
    @just --list

_local-sase-source:
    @printf '%s\n' {{ quote(local_sase_source) }}

_local-sase-core-source:
    @printf '%s\n' {{ quote(local_sase_core_source) }}

_validate-local-sase:
    @sase_src={{ quote(local_sase_source) }}; \
    if [ ! -f "$sase_src/pyproject.toml" ] || [ ! -d "$sase_src/src/sase" ]; then \
        printf '%s\n' "Local SASE source checkout not found at: $sase_src" >&2; \
        printf '%s\n' "Set SASE_TELEGRAM_SASE_SOURCE_DIR=/path/to/sase, create ../sase next to this repo, run inside a SASE linked workspace, or check out .sase-deps/sase in CI." >&2; \
        exit 1; \
    fi

_validate-local-sase-core:
    @core_src={{ quote(local_sase_core_source) }}; \
    if [ -z "$core_src" ] || [ ! -f "$core_src/crates/sase_core_py/pyproject.toml" ]; then \
        printf '%s\n' "Local SASE core checkout not found at: $core_src" >&2; \
        printf '%s\n' "Set SASE_TELEGRAM_SASE_CORE_SOURCE_DIR=/path/to/sase-core, create ../sase-core next to this repo, or check out .sase-deps/sase-core in CI." >&2; \
        exit 1; \
    fi

_ensure-venv:
    @[ -x {{ quote(venv_python) }} ] || uv venv {{ quote(venv_dir) }}

_install-local-sase-core: _validate-local-sase-core _ensure-venv
    @[ -x {{ quote(venv_maturin) }} ] || uv pip install --python {{ quote(venv_python) }} maturin
    cd {{ quote(local_sase_core_py_source) }} && VIRTUAL_ENV={{ quote(venv_path) }} PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 {{ quote(venv_maturin) }} develop --release

# Route the `sase` requirement to the coordinated local checkout with a uv
# --overrides file so the install resolves without hitting unresolvable PyPI
# releases. sase-core-rs still resolves from PyPI first and is then overwritten
# by _install-local-sase-core's maturin build of the coordinated sase-core checkout.
_write-sase-overrides: _validate-local-sase
    @printf -- '-e %s\n' {{ quote(local_sase_source) }} > {{ quote(sase_overrides_file) }}

_setup: _validate-local-sase _validate-local-sase-core _write-sase-overrides
    @if [ ! -x {{ quote(venv_python) }} ]; then \
        uv venv {{ quote(venv_dir) }}; \
        uv pip install --python {{ quote(venv_python) }} --overrides {{ quote(sase_overrides_file) }} -e ".[dev]"; \
    fi
    just _install-local-sase-core

install: _validate-local-sase _validate-local-sase-core _ensure-venv _write-sase-overrides
    uv pip install --python {{ quote(venv_python) }} --overrides {{ quote(sase_overrides_file) }} -e ".[dev]"
    just _install-local-sase-core

# Install a source-overridden sase plus a coordinated sase-core-rs build into
# the venv rooted at the given python interpreter (used by the wheel smoke
# test, after the built sase-telegram wheel has already been installed with
# an equivalent --overrides file pointed at the local sase source).
install-source-sase python: _validate-local-sase _validate-local-sase-core
    #!/usr/bin/env bash
    set -euo pipefail
    uv pip install --python "{{ python }}" --no-deps -e {{ quote(local_sase_source) }}
    uv pip install --python "{{ python }}" maturin
    venv_root="$(cd "$(dirname "{{ python }}")/.." && pwd)"
    cd {{ quote(local_sase_core_py_source) }}
    VIRTUAL_ENV="$venv_root" PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 "$venv_root/bin/maturin" develop --release

lint: _setup
    {{ venv_bin }}/ruff check src/ tests/
    {{ venv_bin }}/mypy

fmt: _setup
    {{ venv_bin }}/ruff format src/ tests/
    {{ venv_bin }}/ruff check --fix src/ tests/

test *args: _setup
    {{ venv_bin }}/pytest {{ args }}

check: lint test

clean:
    rm -rf build/ dist/ *.egg-info src/*.egg-info .mypy_cache/ .ruff_cache/ .pytest_cache/

build: _setup
    {{ venv_bin }}/python -m build
