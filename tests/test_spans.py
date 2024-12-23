from tests import conftest


def test_span(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert set(spans.keys()) == {
        "test run",
        "test_span.py::test_pass",
        "test_span.py::test_pass::setup",
        "test_span.py::test_pass::call",
        "test_span.py::test_pass::teardown",
    }
