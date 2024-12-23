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


PytesterWithSpanReturnT = tuple[_pytest.pytester.RunResult, list[trace.ReadableSpan]]


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
        monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)
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
        return result, spans

    return _run
