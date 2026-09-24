"""Keep the README first screen honest: plain string checks, no markdown parser."""

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
MAX_BADGES = 4
LABELS = (
    "**What it does**",
    "**Who it's for**",
    "**What stays on your device / what leaves it**",
    "**Runs on**",
    "**Not for**",
    "**Status**",
)


def _first_screen() -> str:
    return README.split("## Try it in 60 seconds", 1)[0]


def _tagline() -> str:
    lines = [line for line in README.splitlines()[1:] if line.strip()]
    return next(line for line in lines if not line.startswith("[!["))


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```text\n(.*?)```", text, flags=re.DOTALL)


def test_title_and_tagline_match_the_package_description() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert README.splitlines()[0] == "# Agentic Context Service"
    assert len(_tagline()) <= MAX_TAGLINE
    assert _tagline() == project["description"]


def test_first_screen_has_few_badges_and_every_label() -> None:
    before_glance = README.split("## At a glance", 1)[0]
    assert before_glance.count("[![") <= MAX_BADGES
    missing = [label for label in LABELS if label not in _first_screen()]
    assert not missing, f"first screen is missing labels: {missing}"


def test_sections_and_hero_caption_are_in_order() -> None:
    caption = README.index("Real output of the example below")
    try_it = README.index("## Try it in 60 seconds")
    assert caption < try_it < README.index("## How it works")


def test_status_matches_the_untagged_pre_release_version() -> None:
    status = next(line for line in README.splitlines() if line.startswith("- **Status**"))
    assert status.startswith("- **Status** — Beta (pre-1.0)")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"].startswith("0.")


def test_architecture_map_is_linked_and_its_source_exists() -> None:
    assert f"[Explore the interactive architecture map →]({ARCHITECTURE_MAP})" in README
    assert (ROOT / ARCHITECTURE_MAP).is_file()
    assert (ROOT / ARCHITECTURE_SOURCE).is_file()


def test_every_relative_link_resolves() -> None:
    targets = re.findall(r"\]\(([^)\s]+)\)", README)
    relative = [
        target.split("#", 1)[0]
        for target in targets
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]
    missing = sorted({target for target in relative if not (ROOT / target).exists()})
    assert not missing, f"README links to missing paths: {missing}"


def test_hero_and_example_output_are_the_real_output(capsys: pytest.CaptureFixture[str]) -> None:
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    actual = capsys.readouterr().out
    blocks = _fenced_blocks(README)
    assert actual in blocks, "the Try-it output block must equal a real run of the example"
    hero = blocks[0].split("output: ", 1)[1]
    assert [line.strip() for line in hero.splitlines()] == [
        line.strip() for line in actual.splitlines()
    ]
