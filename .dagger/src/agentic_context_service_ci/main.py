"""Exact-source quality and security gates for Agentic Context Service."""

from __future__ import annotations

from typing import Annotated, Final

import dagger
from dagger import Ignore, check, dag, function, object_type

PYTHON_IMAGE: Final = (
    "python:3.13.14-bookworm@sha256:"
    "8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f"
)
UV_IMAGE: Final = (
    "ghcr.io/astral-sh/uv:0.11.32@sha256:"
    "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
)
REPOSITORY: Final = "hseshadr/agentic-context-service"
REPOSITORY_URL: Final = "https://github.com/hseshadr/agentic-context-service.git"
SOURCE_ROOT: Final = "/src"
LOCK_INPUTS: Final = ("pyproject.toml", "uv.lock")
SOURCE_IGNORE_PATTERNS: Final = [
    ".git",
    ".env",
    "**/.env",
    ".env.*",
    "**/.env.*",
    "!.env.example",
    "!**/.env.example",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/*.key",
    "**/*.jks",
    "**/*.p12",
    "**/*.pfx",
    "**/*.pem",
    "**/*.tfstate",
    "**/*.tfvars",
    "**/*credentials*",
    "**/*secret*",
    ".artifacts",
    ".dagger/.venv",
    ".dagger/sdk",
    ".coverage*",
    "**/.coverage*",
    ".hypothesis",
    "**/.hypothesis",
    ".mypy_cache",
    "**/.mypy_cache",
    ".pytest_cache",
    "**/.pytest_cache",
    ".ruff_cache",
    "**/.ruff_cache",
    "**/__pycache__",
    ".venv",
    "build",
    "dist",
]


async def _guard(
    source: dagger.Directory,
    commit_sha: str,
    git_auth_header: dagger.Secret | None,
) -> None:
    guarded = dag.foundation().guard(
        source=source,
        repository=REPOSITORY,
        commit_sha=commit_sha,
        http_auth_header=git_auth_header,
    )
    await guarded.sync()


async def _exact_source(
    source: dagger.Directory,
    commit_sha: str,
    git_auth_header: dagger.Secret | None,
) -> dagger.Directory:
    await _guard(source, commit_sha, git_auth_header)
    repository = dag.git(REPOSITORY_URL, http_auth_header=git_auth_header)
    return repository.commit(commit_sha).tree(depth=0, include_tags=True)


def _dependencies(source: dagger.Directory) -> dagger.Container:
    uv = dag.container().from_(UV_IMAGE).file("/uv")
    base = dag.container().from_(PYTHON_IMAGE).with_file("/usr/local/bin/uv", uv)
    locked = base.with_directory(SOURCE_ROOT, source, include=list(LOCK_INPUTS))
    locked = locked.with_workdir(SOURCE_ROOT).with_env_variable(
        "UV_PROJECT_ENVIRONMENT", "/opt/venv"
    )
    return locked.with_exec(
        ["uv", "sync", "--frozen", "--all-groups", "--all-extras", "--no-install-project"]
    )


def _project(source: dagger.Directory) -> dagger.Container:
    complete = _dependencies(source).with_directory(SOURCE_ROOT, source).with_workdir(SOURCE_ROOT)
    # `_dependencies` syncs with --no-install-project, so the project itself is built
    # here. Build isolation would re-resolve `build-system.requires` against index
    # metadata the lockfile path never cached, which --offline cannot reach:
    # "hatchling was not found in the cache". hatchling is already installed from
    # uv.lock, so skip isolation. A wheel rather than an editable install then avoids
    # `editables`, which uv.lock does not carry at all.
    return complete.with_exec(
        [
            "uv",
            "sync",
            "--frozen",
            "--all-groups",
            "--all-extras",
            "--offline",
            "--no-build-isolation",
            "--no-editable",
        ]
    )


@object_type
class AgenticContextService:
    """Expose only the canonical CI and security operations."""

    @function
    @check
    async def ci(
        self,
        source: Annotated[dagger.Directory, Ignore(SOURCE_IGNORE_PATTERNS)],
        commit_sha: str,
        git_auth_header: dagger.Secret | None = None,
    ) -> str:
        """Resolve the guarded commit once and run the repository-owned gate."""
        verified = await _exact_source(source, commit_sha, git_auth_header)
        proof = _project(verified).with_exec(
            ["uv", "run", "python", "-m", "scripts.validate_contracts"]
        )
        await proof.with_exec(["uv", "run", "poe", "verify"]).sync()
        return "Agentic Context Service canonical Dagger gate passed"

    @function
    async def security(
        self,
        source: Annotated[dagger.Directory, Ignore(SOURCE_IGNORE_PATTERNS)],
        commit_sha: str,
        git_auth_header: dagger.Secret | None = None,
    ) -> str:
        """Run guarded locked dependency and source security checks."""
        verified = await _exact_source(source, commit_sha, git_auth_header)
        audit = dag.python_package().dependency_audit(
            source=verified,
            repository=REPOSITORY,
            commit_sha=commit_sha,
            http_auth_header=git_auth_header,
        )
        await audit.sync()
        await (
            _project(verified)
            .with_exec(["uv", "run", "bandit", "-q", "-r", "src", "scripts"])
            .sync()
        )
        return "Agentic Context Service dependency and source audits passed"
