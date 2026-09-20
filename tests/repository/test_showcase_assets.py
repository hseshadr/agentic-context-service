"""Static contract checks for the server-wired showcase dashboard."""

from pathlib import Path

_SHOWCASE = (
    Path(__file__).parents[2] / "src" / "agentic_context_service" / "api" / "static" / "showcase"
)


def test_showcase_keeps_the_four_observable_lanes() -> None:
    page = (_SHOWCASE / "index.html").read_text(encoding="utf-8")

    for lane in ("source", "cdc", "agent", "transaction"):
        assert f'data-lane="{lane}"' in page

    assert 'href="#flow"' in page
    assert 'aria-live="polite"' in page


def test_showcase_uses_the_redacted_snapshot_and_sse_contract() -> None:
    script = (_SHOWCASE / "app.js").read_text(encoding="utf-8")

    assert 'endpoint(runId, "/events")' in script
    assert "new EventSource(url)" in script
    assert "public_metadata" in script
    assert "SAFE_METADATA" in script
    assert "public_title" in script
    assert '"transition"' in script
    assert '"tool_name"' in script


def test_showcase_respects_reduced_motion() -> None:
    styles = (_SHOWCASE / "styles.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion: reduce" in styles
