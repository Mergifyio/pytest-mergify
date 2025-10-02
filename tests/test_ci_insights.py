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
    result.assert_outcomes(
        passed=2 + (2 * 1000),  # 2 initial runs, 1000 retries for each test.
        failed=1,  # Only the first run of the flaky test.
        skipped=1,  # Skipped tests are excluded from flaky detection.
    )

    assert "Flaky detection is active" in result.stdout.str()

    assert spans is not None
    assert len(spans) == 2005
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


@pytest.mark.parametrize(
    argnames=[
        "total_test_durations_ms",
        "test_duration_ms",
        "expected_retry_count",
    ],
    argvalues=[
        pytest.param(
            1000,
            3,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(0, 1)],
            id="Very fast test",
        ),
        pytest.param(
            1000,
            20,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(1, 5)],
            id="Fast test",
        ),
        pytest.param(
            1000,
            70,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(5, 10)],
            id="Moderate test",
        ),
        pytest.param(
            1000,
            500,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(10, 100)],
            id="Slow test",
        ),
        pytest.param(
            2000,
            3,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(0, 1)],
            id="Very fast test with higher total duration",
        ),
        pytest.param(
            20000,
            20,
            ci_insights._DEFAULT_TEST_RETRY_BUDGET[range(0, 1)],
            id="Fast test with higher total duration",
        ),
    ],
)
def test_compute_test_retry_count(
    total_test_durations_ms: int,
    test_duration_ms: int,
    expected_retry_count: int,
) -> None:
    assert (
        ci_insights._get_retry_count_for_cost(
            ci_insights._compute_test_retry_cost(
                total_test_durations_ms=total_test_durations_ms,
                test_duration_ms=test_duration_ms,
            )
        )
        == expected_retry_count
    )
