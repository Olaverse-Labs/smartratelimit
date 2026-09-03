"""Tests for the browser playground's embedded Python bridge.

The playground page runs the real library under Pyodide, which means it carries
a small block of Python calling `smartratelimit.simulate`. That block lives in a
Markdown file, so nothing would normally compile it — and a rename in the
library would break the page silently, discovered only by a visitor.

These tests extract that exact block and run it under CPython. They cannot prove
Pyodide works, but they do prove the page and the library still agree on an API.
"""

import json
import pathlib
import re

import pytest

PAGE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "playground.md"

def _render_source():
    """The body of the page's JavaScript renderer."""
    page = PAGE.read_text()
    start = page.index("  function render(r) {")
    end = page.index("  function run() {", start)
    return page[start:end]


def _fields_read_off(variable, source):
    """Property names the renderer reads off ``variable``, minus method calls."""
    pattern = r"\b" + variable + r"\.([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\()"
    return set(re.findall(pattern, source))


# Derived from the renderer rather than listed by hand. A hand-kept list is one
# more thing to forget: the columns gained RAW LIMIT and COST/REQ while this list
# stayed as it was, so the test went on passing while the page rendered "NaN".
_RENDER = _render_source()
REQUIRED_KEYS = _fields_read_off("r", _RENDER)
REQUIRED_BUDGET_KEYS = _fields_read_off("b", _RENDER)


def extract_bridge():
    """Reconstruct the Python the page hands to Pyodide, exactly as the JS does."""
    page = PAGE.read_text()
    block = re.search(
        r'pyodide\.runPython\(\[\n(.*?)\n      \]\.join\("\\n"\)\);', page, re.S
    )
    assert block, "could not find the embedded Python block in playground.md"

    lines = re.findall(r'^\s*"(.*?)",\s*$', block.group(1), re.M)
    assert lines, "embedded Python block is empty"
    return "\n".join(l.replace('\\"', '"').replace("\\\\", "\\") for l in lines)


@pytest.fixture(scope="module")
def bridge():
    namespace = {}
    exec(compile(extract_bridge(), "<playground>", "exec"), namespace)
    return namespace


class TestBridgeStillMatchesTheLibrary:
    """A rename in the library must fail here, not in someone's browser."""

    def test_it_compiles_and_defines_the_entry_point(self, bridge):
        assert callable(bridge["run_simulation"])

    def test_it_produces_the_payload_the_renderer_reads(self, bridge):
        # The renderer is JavaScript: a key the payload omits is `undefined`,
        # which formats as "NaN" rather than raising. Nothing on the page would
        # tell you, so this is where it has to be caught.
        assert REQUIRED_KEYS, "scraped no fields off the renderer"
        assert REQUIRED_BUDGET_KEYS, "scraped no budget fields off the renderer"

        payload = json.loads(bridge["run_simulation"](500, 100000, 2000, 1000, 20, 1, 0))

        assert not REQUIRED_KEYS - set(payload), (
            f"renderer reads {sorted(REQUIRED_KEYS - set(payload))} "
            f"but the bridge does not send it"
        )
        budget = payload["budgets"][0]
        assert not REQUIRED_BUDGET_KEYS - set(budget), (
            f"renderer reads {sorted(REQUIRED_BUDGET_KEYS - set(budget))} off each "
            f"budget but the bridge does not send it"
        )

    def test_every_number_the_renderer_prints_is_a_number(self, bridge):
        """`undefined` arithmetic yields NaN, which prints without complaint."""
        payload = json.loads(bridge["run_simulation"](4200, 128000, 2000, 1000, 20, 2, 3))

        for budget in payload["budgets"]:
            for key in ("limit", "window_seconds", "cost_per_request",
                        "ceiling_per_minute", "utilisation"):
                value = budget[key]
                assert isinstance(value, (int, float)), (budget["name"], key, value)
                assert value == value, (budget["name"], key)  # not NaN

    def test_it_agrees_with_the_library_it_wraps(self, bridge):
        """The page must not quietly compute something of its own."""
        from datetime import timedelta

        from smartratelimit.simulate import Budget, simulate

        direct = simulate(
            [
                Budget("requests", 500, timedelta(minutes=1), cost=1.0),
                Budget("tokens", 100_000, timedelta(minutes=1), cost=2000.0),
            ],
            requests=1000,
            workers=20,
        )
        via_page = json.loads(bridge["run_simulation"](500, 100000, 2000, 1000, 20, 1, 0))

        assert via_page["binding"] == direct.binding
        assert via_page["throttled"] == direct.throttled
        assert via_page["wall_seconds"] == pytest.approx(direct.wall_seconds)

    def test_the_json_is_serialisable_for_an_instantaneous_run(self, bridge):
        """achieved_rps is None there; the renderer branches on null."""
        payload = json.loads(bridge["run_simulation"](500, 100000, 100, 20, 1, 1, 0))

        assert payload["achieved_rps"] is None

    def test_latency_surfaces_a_concurrency_ceiling(self, bridge):
        payload = json.loads(bridge["run_simulation"](500, 10_000_000, 1, 200, 3, 1, 2))

        assert payload["concurrency_ceiling_rps"] == pytest.approx(1.5)

    def test_an_impossible_scenario_raises_something_catchable(self, bridge):
        """The page shows the message; it must not be a bare traceback."""
        with pytest.raises(ValueError, match="no request can ever be sent"):
            bridge["run_simulation"](500, 1000, 5000, 5, 1, 1, 0)


class TestPublishedModulesMatchTheLibrary:
    """The page's premise is that it runs the library, not a copy of it."""

    def test_the_hook_publishes_them_verbatim(self, tmp_path):
        """A drifted copy would make every number on the page a claim, not a fact."""
        import sys

        sys.path.insert(0, str(PAGE.parent.parent / "hooks"))
        try:
            import playground_modules
        finally:
            sys.path.pop(0)

        playground_modules.on_post_build({"site_dir": str(tmp_path)})
        published = tmp_path / "assets" / "py" / "smartratelimit"
        package = PAGE.parent.parent / "smartratelimit"

        for name in playground_modules.MODULES:
            assert (published / name).read_bytes() == (package / name).read_bytes(), (
                f"{name} published to the playground differs from the package"
            )

    def test_only_the_package_init_is_stubbed(self):
        """Everything that computes anything must be the real file."""
        import sys

        sys.path.insert(0, str(PAGE.parent.parent / "hooks"))
        try:
            import playground_modules
        finally:
            sys.path.pop(0)

        assert "__init__.py" not in playground_modules.MODULES
        assert "simulate.py" in playground_modules.MODULES

    def test_the_published_modules_need_no_third_party_imports(self):
        """The whole approach rests on them being stdlib-only under Pyodide."""
        import re
        import sys

        sys.path.insert(0, str(PAGE.parent.parent / "hooks"))
        try:
            import playground_modules
        finally:
            sys.path.pop(0)

        package = PAGE.parent.parent / "smartratelimit"
        allowed = {"dataclasses", "datetime", "typing", "json", "enum", "math"}

        for name in playground_modules.MODULES:
            for line in (package / name).read_text().splitlines():
                match = re.match(r"^(?:from|import)\s+([\w.]+)", line)
                if not match:
                    continue
                root = match.group(1).split(".")[0]
                assert root in allowed or root == "smartratelimit", (
                    f"{name} imports {root!r}, which Pyodide may not provide"
                )


class TestPageHonesty:
    """The claims the page makes about itself have to stay true."""

    def test_it_runs_the_library_not_a_reimplementation(self):
        page = PAGE.read_text()

        assert "from smartratelimit.simulate import" in page

    def test_it_explains_why_it_does_not_install_the_package(self):
        """sqlite3 is unvendored under Pyodide; readers deserve the reason."""
        page = PAGE.read_text()

        assert "sqlite3" in page

    def test_it_names_an_offline_fallback_when_the_cdn_is_blocked(self):
        page = PAGE.read_text()

        assert "smartratelimit simulate" in page
