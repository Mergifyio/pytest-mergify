import re
import typing

import pytest
import responses

from pytest_mergify import ci_insights

from . import conftest


def _set_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_MERGIFY_TEST_NEW_FLAKY_DETECTION", "true")
    monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_API_URL", "https://example.com")
    monkeypatch.setenv("MERGIFY_TOKEN", "my_token")


def _make_quarantine_mock() -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/repositories/pytest-mergify/quarantines",
        json={"quarantined_tests": []},
        status=200,
    )


def _make_test_names_mock(test_names: typing.List[str] = [], status: int = 200) -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/tests/names",
        json={"test_names": test_names},
        status=status,
    )


def _make_test_client() -> ci_insights.MergifyCIInsights:
    return ci_insights.MergifyCIInsights(
        token="my_token",
        repo_name="Mergifyio/pytest-mergify",
        api_url="https://example.com",
    )


@responses.activate
def test_load_flaky_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(test_names=["a::test_a", "b::test_b"])

    client = _make_test_client()
    assert not client.flaky_detection_error_message
    assert client.existing_test_names == ["a::test_a", "b::test_b"]


@responses.activate
def test_load_flaky_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(status=500)

    client = _make_test_client()
    assert not client.existing_test_names
    assert client.flaky_detection_error_message is not None
    assert "500 Server Error" in client.flaky_detection_error_message


@responses.activate
def test_flaky_detection_detects_new_tests(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_test_names_mock(
        [
            "test_flaky_detection_detects_new_tests.py::test_foo",
            "test_flaky_detection_detects_new_tests.py::test_unknown",
        ]
    )

    result, spans = pytester_with_spans(
        code="""
        import pytest

        def test_foo():
            assert True

        execution_count = 0

        def test_bar():
            # Simulate a flaky test.
            global execution_count
            if execution_count == 2:
                pytest.fail("I'm flaky!")
            
            execution_count += 1
        
        def test_baz():
            assert True

        def test_qux():
            pytest.skip("I'm skipped!")
        """
    )

    assert re.search(
        r"""Fetched 2 existing tests
Detected 3 new tests
  - test_flaky_detection_detects_new_tests\.py::test_bar \(\d+ms\)
  - test_flaky_detection_detects_new_tests\.py::test_baz \(\d+ms\)
  - test_flaky_detection_detects_new_tests\.py::test_qux \(\d+ms\)
Detected 1 new flaky tests
  - test_flaky_detection_detects_new_tests\.py::test_bar""",
        result.stdout.str(),
        re.MULTILINE,
    )

    assert spans is not None
    for test_name, expected in {
        "test_flaky_detection_detects_new_tests.py::test_foo": False,
        "test_flaky_detection_detects_new_tests.py::test_bar": True,
        "test_flaky_detection_detects_new_tests.py::test_baz": True,
        "test_flaky_detection_detects_new_tests.py::test_qux": False,
    }.items():
        span = spans.get(test_name)

        assert span is not None
        assert span.attributes is not None
        if expected:
            assert span.attributes.get("cicd.test.new")
