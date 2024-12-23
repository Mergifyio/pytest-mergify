import typing

import pytest
import _pytest.pytester
from opentelemetry.sdk import trace

import pytest_mergify

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def set_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Always override API
    monkeypatch.setenv("MERGIFY_API_URL", "http://localhost:9999")


PytesterWithSpanReturnT = tuple[
    _pytest.pytester.RunResult, dict[str, trace.ReadableSpan]
]


class PytesterWithSpanT(typing.Protocol):
    def __call__(self, code: str = ..., /) -> PytesterWithSpanReturnT: ...


_DEFAULT_PYTESTER_CODE = "def test_pass(): pass"


@pytest.fixture
def pytester_with_spans(
    pytester: _pytest.pytester.Pytester, monkeypatch: pytest.MonkeyPatch
) -> PytesterWithSpanT:
    def _run(
        code: str = _DEFAULT_PYTESTER_CODE,
    ) -> PytesterWithSpanReturnT:
        monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")
        plugin = pytest_mergify.PytestMergify()
        pytester.makepyfile(code)
        result = pytester.runpytest_inprocess(plugins=[plugin])
        if code is _DEFAULT_PYTESTER_CODE:
            result.assert_outcomes(passed=1)
        assert plugin.mergify_tracer.exporter is not None
        spans: list[trace.ReadableSpan] = (
            plugin.mergify_tracer.exporter.get_finished_spans()  # type: ignore[attr-defined]
        )
        spans_as_dict = {s.name: s for s in spans}
        # Make sure we don't lose spans in the process
        assert len(spans_as_dict) == len(spans)
        return result, spans_as_dict

    return _run
