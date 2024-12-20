from tests import conftest


def test_span(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert any(s.name == "test run" for s in spans)
    assert any(s.name == "test_span.py::test_pass" for s in spans)
    assert any(s.name == "test_span.py::test_pass::setup" for s in spans)
    assert any(s.name == "test_span.py::test_pass::call" for s in spans)
    assert any(s.name == "test_span.py::test_pass::teardown" for s in spans)
