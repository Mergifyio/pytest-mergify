import datetime
import typing

import pytest

from pytest_mergify import flaky_detection

_MIN_TEST_RETRY_COUNT = 5
_MAX_TEST_RETRY_COUNT = 1000


@pytest.mark.parametrize(
    argnames=["budget_duration", "test_durations", "expected_allocation"],
    argvalues=[
        pytest.param(
            datetime.timedelta(seconds=30),
            {
                "test_a": datetime.timedelta(milliseconds=10),
                "test_b": datetime.timedelta(milliseconds=20),
                "test_c": datetime.timedelta(milliseconds=30),
                "test_d": datetime.timedelta(milliseconds=40),
            },
            {
                "test_a": 750,
                "test_b": 375,
                "test_c": 250,
                "test_d": 187,
            },
            id="Basic",
        ),
        pytest.param(
            datetime.timedelta(seconds=10),
            {
                "test_a": datetime.timedelta(milliseconds=10),
                "test_b": datetime.timedelta(milliseconds=2),
                "test_c": datetime.timedelta(milliseconds=100),
                "test_d": datetime.timedelta(seconds=10),  # Not affordable.
                "test_e": datetime.timedelta(milliseconds=1),
                "test_f": datetime.timedelta(milliseconds=100),
            },
            {
                "test_a": 233,
                "test_b": _MAX_TEST_RETRY_COUNT,
                "test_c": 23,
                "test_e": _MAX_TEST_RETRY_COUNT,
                "test_f": 23,
            },
            id="With slow and fast tests",
        ),
        pytest.param(
            datetime.timedelta(seconds=10),
            {},
            {},
            id="Empty test durations",
        ),
        pytest.param(
            datetime.timedelta(),
            {"test_a": datetime.timedelta(milliseconds=1)},
            {},
            id="Empty budget duration",
        ),
        pytest.param(
            datetime.timedelta(milliseconds=1),
            {
                "test_a": datetime.timedelta(seconds=1),
                "test_b": datetime.timedelta(seconds=2),
            },
            {},
            id="Budget too small",
        ),
        pytest.param(
            datetime.timedelta(seconds=5),
            {"test_a": datetime.timedelta(seconds=1)},
            {"test_a": 5},
            id="Exact budget for minimum retries",
        ),
        pytest.param(
            datetime.timedelta(seconds=2000),
            {"test_a": datetime.timedelta(milliseconds=1)},
            {"test_a": 1000},
            id="Test reaching maximum retries",
        ),
    ],
)
def test_allocate_test_retries(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
    expected_allocation: typing.Dict[str, int],
) -> None:
    allocation = flaky_detection._allocate_test_retries(
        budget_duration, test_durations, _MIN_TEST_RETRY_COUNT, _MAX_TEST_RETRY_COUNT
    )

    total_duration = datetime.timedelta()

    for test, retry_count in allocation.items():
        assert test in test_durations
        assert retry_count >= _MIN_TEST_RETRY_COUNT
        assert retry_count <= _MAX_TEST_RETRY_COUNT

        total_duration += test_durations[test] * retry_count

    # We want to make sure that we are never consuming more than the allocated
    # budget.
    assert total_duration <= budget_duration

    assert allocation == expected_allocation
