import re
import typing

import pytest
import responses

from pytest_mergify import ci_insights, flaky_detection

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
    assert not client.flaky_detector_error_message
    assert client.flaky_detector is not None
    assert client.flaky_detector._existing_tests == ["a::test_a", "b::test_b"]


@responses.activate
def test_load_flaky_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(status=500)

    client = _make_test_client()
    assert client.flaky_detector is None
    assert client.flaky_detector_error_message is not None
    assert "500 Server Error" in client.flaky_detector_error_message


@responses.activate
def test_load_flaky_detection_error_without_existing_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock([])

    client = _make_test_client()
    assert client.flaky_detector is None
    assert client.flaky_detector_error_message is not None
    assert (
        "No existing tests found for 'Mergifyio/pytest-mergify' repository on branch 'main'"
        in client.flaky_detector_error_message
    )


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
        code=f"""
        import pytest

        SESSION_ALREADY_SET = False

        @pytest.fixture(scope="session", autouse=True)
        def _setup_session() -> None:
            global SESSION_ALREADY_SET
            if SESSION_ALREADY_SET:
                raise RuntimeError("This function should not be called twice")
            SESSION_ALREADY_SET = True

        @pytest.fixture(autouse=True)
        def _setup_test() -> None:
            pass

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

        def test_quux_{"a" * (flaky_detection._MAX_TEST_NAME_LENGTH + 10)}():
            assert True

        def test_corge():
            assert True
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
- Skipped 1 test\(s\):
    • 'test_flaky_detection\.py::test_quux_[a]+' has not been tested multiple times because the name of the test exceeds our limit of \d+ characters
- Used [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
- Active for 3 new test\(s\):
    • 'test_flaky_detection\.py::test_bar' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection\.py::test_baz' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection\.py::test_corge' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)""",
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


@responses.activate
def test_flaky_detection_with_only_one_new_test_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_test_names_mock(
        ["test_flaky_detection_with_only_one_new_test_at_the_end.py::test_foo"]
    )

    result, _ = pytester_with_spans(
        code="""
        import pytest

        SESSION_ALREADY_SET = False

        @pytest.fixture(scope="session", autouse=True)
        def _setup_session() -> None:
            global SESSION_ALREADY_SET
            if SESSION_ALREADY_SET:
                raise RuntimeError("This function should not be called twice")
            SESSION_ALREADY_SET = True

        def test_foo():
            assert True

        def test_corge():
            assert True
        """
    )
    result.assert_outcomes(passed=1002)
