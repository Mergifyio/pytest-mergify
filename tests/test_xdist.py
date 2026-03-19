import typing

import _pytest.pytester
import pytest
import responses

import pytest_mergify
from pytest_mergify import flaky_detection
from tests.test_ci_insights import (
    _make_flaky_detection_context_mock,
    _make_quarantine_mock,
    _set_test_environment,
)


pytest_plugins = ["pytester"]


@responses.activate
def test_flaky_detection_new_tests_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    """Flaky detection detects new tests end-to-end (non-xdist, validates shared report path)."""
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_new_tests_end_to_end.py::test_existing",
        ],
    )

    pytester.makepyfile(
        """
        def test_existing():
            assert True

        def test_new_a():
            assert True

        def test_new_b():
            assert True
        """
    )

    result = pytester.runpytest_inprocess(
        plugins=[pytest_mergify.PytestMergify()],
    )

    # Verify flaky detection report is present.
    result.stdout.fnmatch_lines(["*Flaky detection*"])
    result.stdout.fnmatch_lines(["*Active for 2 new test*"])
    result.stdout.fnmatch_lines(["*test_new_a*has been tested*"])
    result.stdout.fnmatch_lines(["*test_new_b*has been tested*"])


def test_xdist_worker_flow_from_context_to_metrics() -> None:
    """Simulate the full xdist worker lifecycle: context -> prepare -> serialize."""
    context_dict: typing.Dict[str, typing.Any] = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_existing"],
        "existing_tests_mean_duration_ms": 10000,
        "unhealthy_test_names": [],
        "max_test_execution_count": 1000,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }

    detector = flaky_detection.FlakyDetector.from_context(
        context_dict=context_dict,
        mode="new",
    )

    assert detector._is_xdist is True

    # Simulate serialization round-trip.
    metrics = detector.to_serializable_metrics()
    assert isinstance(metrics, dict)
    assert "test_metrics" in metrics
    assert "over_length_tests" in metrics
    assert "debug_logs" in metrics


def test_xdist_aggregated_report() -> None:
    """Controller generates correct report from aggregated worker metrics."""
    context_dict: typing.Dict[str, typing.Any] = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_existing"],
        "existing_tests_mean_duration_ms": 10000,
        "unhealthy_test_names": [],
        "max_test_execution_count": 1000,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }

    # Simulate metrics from two workers.
    worker1_metrics: typing.Dict[str, typing.Any] = {
        "test_new_a": {
            "rerun_count": 10,
            "total_duration_ms": 500.0,
            "initial_setup_duration_ms": 5.0,
            "initial_call_duration_ms": 40.0,
            "initial_teardown_duration_ms": 5.0,
            "prevented_timeout": False,
        },
    }
    worker2_metrics: typing.Dict[str, typing.Any] = {
        "test_new_b": {
            "rerun_count": 8,
            "total_duration_ms": 400.0,
            "initial_setup_duration_ms": 5.0,
            "initial_call_duration_ms": 45.0,
            "initial_teardown_duration_ms": 5.0,
            "prevented_timeout": False,
        },
    }

    aggregated: typing.Dict[str, typing.Any] = {
        "test_metrics": {**worker1_metrics, **worker2_metrics},
        "over_length_tests": [],
        "debug_logs": [],
    }

    report = flaky_detection.make_report_from_aggregated(
        context_dict=context_dict,
        mode="new",
        available_budget_duration_ms=4000.0,
        aggregated_metrics=aggregated,
    )

    assert "Flaky detection" in report
    assert "test_new_a" in report
    assert "test_new_b" in report
    assert "Active for 2 new test" in report


@responses.activate
def test_no_crash_without_xdist(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    """Plugin works normally without xdist."""
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=["test_no_crash_without_xdist.py::test_pass"],
    )

    pytester.makepyfile(
        """
        def test_pass():
            assert True
        """
    )

    result = pytester.runpytest_inprocess(
        plugins=[pytest_mergify.PytestMergify()],
    )
    result.assert_outcomes(passed=1)


def test_flaky_detection_disabled_under_each_mode() -> None:
    """Controller does not distribute flaky context under 'each' scheduling."""
    plugin = pytest_mergify.PytestMergify()
    plugin._xdist_controller = flaky_detection.XdistFlakyDetectionController(
        _context_dict={"existing_test_names": ["test_a"]},
        _mode="new",
    )

    # Mock node with 'each' dist mode.
    class FakeOption:
        dist = "each"

    class FakeConfig:
        option = FakeOption()

    class FakeNode:
        config = FakeConfig()
        workerinput: typing.Dict[str, typing.Any] = {}

    node = FakeNode()
    plugin.pytest_configure_node(node)

    # Context should NOT be distributed.
    assert "flaky_detection_context" not in node.workerinput


def test_flaky_detection_enabled_under_load_mode() -> None:
    """Controller distributes flaky context under 'load' scheduling."""
    plugin = pytest_mergify.PytestMergify()
    plugin._xdist_controller = flaky_detection.XdistFlakyDetectionController(
        _context_dict={"existing_test_names": ["test_a"]},
        _mode="new",
    )

    class FakeOption:
        dist = "load"

    class FakeConfig:
        option = FakeOption()

    class FakeNode:
        config = FakeConfig()
        workerinput: typing.Dict[str, typing.Any] = {}

    node = FakeNode()
    plugin.pytest_configure_node(node)

    # Context should be distributed.
    assert "flaky_detection_context" in node.workerinput
    assert node.workerinput["flaky_detection_mode"] == "new"
