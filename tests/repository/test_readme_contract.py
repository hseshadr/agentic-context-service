"""Keep the README honest and plain: plain string checks, no markdown parser."""

from __future__ import annotations

import re
import runpy
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
EXAMPLE = ROOT / "examples/quickstart/governed_retrieval.py"
ARCHITECTURE_MAP = "docs/architecture/index.html"
ARCHITECTURE_SOURCE = "docs/architecture/governed-context-flow.dataflow.json"
MAX_TAGLINE = 120
MAX_BADGES = 3
SECTIONS = (
    "## Try it",
    "## How it works",
    "## What it does not do",
    "## When to use something else",
    "## Run the full local stack",
    "## Develop",
    "## More detail",
    "## License",
)
TECHNICAL_DOCS = ("docs/ARCHITECTURE.md", "docs/GETTING_STARTED.md")
MORE_DETAIL_LINKS = (
    "docs/ARCHITECTURE.md",
    "docs/GETTING_STARTED.md",
    "docs/architecture/README.md",
    ARCHITECTURE_MAP,
    "docs/specification.md",
    "docs/operations.md",
    "docs/threat-model/README.md",
    "docs/adr/",
    "packages/contracts/openapi.yaml",
    "examples/",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "ROADMAP.md",
)
# Internal vocabulary, hype, and the retired first-screen template. Matched as whole words,
# case-insensitive, so the reader never meets words that mean something only to us.
BANNED = (
    "northstar",
    "seam",
    "lego",
    "trust envelope",
    "receipt",
    "fail-closed",
    "fail closed",
    "gate",
    "fleet",
    "portfolio",
    "production-ready",
    "robust",
    "blazing",
    "enterprise-grade",
    "seamless",
    "at a glance",
    "try it in 60 seconds",
    "below the fold",
)


def _tagline() -> str:
    lines = [line for line in README.splitlines()[1:] if line.strip()]
    return lines[0]


def _intro() -> str:
    return README.split("## Try it", 1)[0]


def _section(heading: str) -> str:
    body = README.split(f"{heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def _fenced_text_blocks() -> list[str]:
    return re.findall(r"```text\n(.*?)```", README, flags=re.DOTALL)


def test_title_and_first_line_match_the_package_description() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert README.splitlines()[0] == "# Agentic Context Service"
    assert len(_tagline()) <= MAX_TAGLINE
    assert _tagline() == project["description"]


def test_fastest_way_to_try_it_is_bold_and_right_under_the_first_line() -> None:
    lines = [line for line in README.splitlines()[1:] if line.strip()]
    assert lines[1].startswith("**"), "the second line must be the bold try-it line"
    assert "examples/quickstart/governed_retrieval.py" in lines[1]


def test_intro_is_short_and_has_few_badges() -> None:
    assert _intro().count("[![") <= MAX_BADGES
    assert "license-MIT" in _intro()


def test_technical_docs_line_sits_in_the_intro() -> None:
    line = next(line for line in _intro().splitlines() if line.startswith("**Technical docs:**"))
    missing = [doc for doc in TECHNICAL_DOCS if f"]({doc})" not in line]
    assert not missing, f"Technical docs line is missing: {missing}"


def test_sections_appear_in_the_standard_order() -> None:
    positions = [README.find(f"\n{heading}\n") for heading in SECTIONS]
    assert -1 not in positions, f"missing sections: {SECTIONS[positions.index(-1)]}"
    assert positions == sorted(positions)


def test_no_internal_jargon_or_hype() -> None:
    lowered = README.lower()
    found = [word for word in BANNED if re.search(rf"\b{re.escape(word)}\b", lowered)]
    assert not found, f"README uses banned wording: {found}"


def test_try_it_output_is_the_real_output(capsys: pytest.CaptureFixture[str]) -> None:
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    actual = capsys.readouterr().out
    assert actual in _fenced_text_blocks(), "the Try it output must equal a real run"
    assert "uv run python examples/quickstart/governed_retrieval.py" in _section("## Try it")


def test_status_is_honest_about_the_untagged_pre_release() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"].startswith("0.")
    limits = _section("## What it does not do")
    assert "Beta" in limits
    assert "not published to PyPI" in limits


def test_develop_links_the_getting_started_guide() -> None:
    develop = _section("## Develop")
    assert "make verify" in develop
    assert "](docs/GETTING_STARTED.md)" in develop


def test_more_detail_links_every_technical_doc() -> None:
    more = _section("## More detail")
    missing = [doc for doc in MORE_DETAIL_LINKS if f"]({doc})" not in more]
    assert not missing, f"More detail is missing links: {missing}"


def test_architecture_map_is_linked_and_its_source_exists() -> None:
    assert f"]({ARCHITECTURE_MAP})" in README
    assert (ROOT / ARCHITECTURE_MAP).is_file()
    assert (ROOT / ARCHITECTURE_SOURCE).is_file()


def test_license_section_says_mit() -> None:
    assert _section("## License").strip().startswith("MIT")


@pytest.mark.parametrize("doc", ["README.md", *TECHNICAL_DOCS])
def test_every_relative_link_resolves(doc: str) -> None:
    path = ROOT / doc
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    assert text, f"{doc} must exist"
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    relative = [
        target.split("#", 1)[0]
        for target in targets
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]
    missing = sorted({t for t in relative if t and not (path.parent / t).exists()})
    assert not missing, f"{doc} links to missing paths: {missing}"
