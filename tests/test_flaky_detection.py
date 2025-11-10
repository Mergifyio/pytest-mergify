import datetime
import typing

import freezegun

from pytest_mergify import flaky_detection

_NOW = datetime.datetime(
    year=2025,
    month=1,
    day=1,
    hour=0,
    minute=0,
    second=0,
    tzinfo=datetime.timezone.utc,
)


class InitializedFlakyDetector(flaky_detection.FlakyDetector):
    def __init__(self) -> None:
        self.token = ""
        self.url = ""
        self.full_repository_name = ""

    def __post_init__(self) -> None:
        pass


def _make_flaky_detection_context(
    budget_ratio: float = 0,
    existing_test_names: typing.List[str] = [],
    existing_tests_mean_duration_ms: int = 0,
    max_test_execution_count: int = 0,
    max_test_name_length: int = 0,
    min_budget_duration_ms: int = 0,
    min_test_execution_count: int = 0,
) -> flaky_detection._FlakyDetectionContext:
    return flaky_detection._FlakyDetectionContext(
        budget_ratio=budget_ratio,
        existing_test_names=existing_test_names,
        existing_tests_mean_duration_ms=existing_tests_mean_duration_ms,
        max_test_execution_count=max_test_execution_count,
        max_test_name_length=max_test_name_length,
        min_budget_duration_ms=min_budget_duration_ms,
        min_test_execution_count=min_test_execution_count,
    )


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_duration_before_deadline() -> None:
    detector = InitializedFlakyDetector()
    detector._new_tests_deadline = _NOW + datetime.timedelta(seconds=10)

    assert detector._get_duration_before_new_tests_deadline() == datetime.timedelta(
        seconds=10
    )


def test_flaky_detector_count_remaining_new_tests() -> None:
    detector = InitializedFlakyDetector()
    detector._new_test_metrics = {
        "foo": flaky_detection._NewTestMetrics(is_processed=True),
        "bar": flaky_detection._NewTestMetrics(),
        "baz": flaky_detection._NewTestMetrics(),
    }
    assert detector._count_remaining_new_tests() == 2


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_retry_count_for_new_tests() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=4000,
        max_test_execution_count=1000,
    )
    detector._new_test_metrics = {
        "foo": flaky_detection._NewTestMetrics(
            initial_duration=datetime.timedelta(milliseconds=10),
            is_processed=True,
        ),
        "bar": flaky_detection._NewTestMetrics(
            initial_duration=datetime.timedelta(milliseconds=100),
        ),
        "baz": flaky_detection._NewTestMetrics(),
    }
    detector.set_deadline()

    assert detector.get_retry_count_for_new_test("bar") == 20


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_retry_count_for_new_tests_with_slow_test() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=500,
        max_test_execution_count=1000,
    )
    detector._new_test_metrics = {
        "foo": flaky_detection._NewTestMetrics(
            # Can't be retried 5 times within the budget.
            initial_duration=datetime.timedelta(seconds=1),
        ),
        "bar": flaky_detection._NewTestMetrics(
            # This test should not be impacted by the previous one.
            initial_duration=datetime.timedelta(milliseconds=1),
        ),
    }
    detector.set_deadline()

    assert detector.get_retry_count_for_new_test("foo") == 0

    assert detector.get_retry_count_for_new_test("bar") == 500


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_retry_count_for_new_tests_with_fast_test() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=4000,
        max_test_execution_count=1000,
    )
    detector._new_test_metrics = {
        "foo": flaky_detection._NewTestMetrics(
            # Should only be retried 1000 times, freeing the rest of the budget for other tests.
            initial_duration=datetime.timedelta(milliseconds=1),
        ),
    }
    detector.set_deadline()

    assert detector.get_retry_count_for_new_test("foo") == 1000
