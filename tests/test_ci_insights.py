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
    assert not client._flaky_detection_error_message
    assert client._existing_test_names == ["a::test_a", "b::test_b"]


@responses.activate
def test_load_flaky_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(status=500)

    client = _make_test_client()
    assert not client._existing_test_names
    assert client._flaky_detection_error_message is not None
    assert "500 Server Error" in client._flaky_detection_error_message


@responses.activate
def test_flaky_detection(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_test_names_mock(
        [
            "test_flaky_detection.py::test_foo",
            "test_flaky_detection.py::test_unknown",
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
            execution_count += 1

            if execution_count == 1:
                pytest.fail("I'm flaky!")

        def test_baz():
            assert True

        def test_qux():
            pytest.skip("I'm skipped!")
        """
    )

    outcomes = result.parseoutcomes()

    # We can't predict the exact number because it depends on the time it takes
    # to run the tests. We just want to make sure that the tests are tested
    # multiple time.
    assert outcomes["passed"] > 1000

    # Only the first run of the flaky test.
    assert outcomes["failed"] == 1

    # The skipped test is tested only once because skipped tests are excluded from the flaky detection.
    assert outcomes["skipped"] == 1

    assert re.search(
        r"""🐛 Flaky detection
- We applied flaky detection on 2 new test\(s\):
    • 'test_flaky_detection\.py::test_bar' has been tested \d+ times using approx\. \d+\.\d+ % of the budget \(\d+ ms/\d+ ms\)
    • 'test_flaky_detection\.py::test_baz' has been tested \d+ times using approx\. \d+\.\d+ % of the budget \(\d+ ms/\d+ ms\)""",
        result.stdout.str(),
        re.MULTILINE,
    )

    assert spans is not None

    # 1 span for the session and one per test.
    assert len(spans) == 1 + sum(outcomes.values())

    for test_name, expected in {
        "test_flaky_detection.py::test_foo": False,
        "test_flaky_detection.py::test_bar": True,
        "test_flaky_detection.py::test_baz": True,
        "test_flaky_detection.py::test_qux": False,
    }.items():
        span = spans.get(test_name)

        assert span is not None
        assert span.attributes is not None
        if expected:
            assert span.attributes.get("cicd.test.new")
