import dataclasses
import typing

import responses

from pytest_mergify import test_selection


API_URL = "https://example.com"
ENDPOINT = f"{API_URL}/v1/ci/owner/repositories/repo/test-selection"


def _make_selection() -> test_selection.TestSelection:
    return test_selection.TestSelection(
        api_url=API_URL,
        token="token",
        repo_name="owner/repo",
        branch_name="mergify/merge-queue/abc",
        head_sha="1" * 40,
        pipeline_name="ci",
        job_name="unit-tests",
    )


@dataclasses.dataclass
class FakeItem:
    nodeid: str


class FakeHook:
    def __init__(self) -> None:
        self.deselected: typing.List[FakeItem] = []

    def pytest_deselected(self, items: typing.List[FakeItem]) -> None:
        self.deselected.extend(items)


@dataclasses.dataclass
class FakeConfig:
    hook: FakeHook = dataclasses.field(default_factory=FakeHook)


@responses.activate
def test_subset_is_applied_to_the_collection() -> None:
    responses.add(
        responses.GET,
        ENDPOINT,
        json={
            "selection": "subset",
            "reason": "reduced_rerun",
            "tests": ["tests/a.py::test_broken", "tests/b.py::test_gone"],
        },
    )
    selection = _make_selection()
    assert selection.selection == "subset"

    items = [
        FakeItem("tests/a.py::test_broken"),
        FakeItem("tests/a.py::test_fine"),
        FakeItem("tests/c.py::test_other"),
    ]
    config = FakeConfig()
    selection.filter_items(config, items)  # type: ignore[arg-type]

    assert [item.nodeid for item in items] == ["tests/a.py::test_broken"]
    assert [item.nodeid for item in config.hook.deselected] == [
        "tests/a.py::test_fine",
        "tests/c.py::test_other",
    ]
    assert selection.kept_count == 1
    assert selection.deselected_count == 2


@responses.activate
def test_subset_matching_nothing_falls_back_to_full() -> None:
    responses.add(
        responses.GET,
        ENDPOINT,
        json={
            "selection": "subset",
            "reason": "reduced_rerun",
            "tests": ["tests/renamed.py::test_gone"],
        },
    )
    selection = _make_selection()

    items = [FakeItem("tests/a.py::test_fine")]
    config = FakeConfig()
    selection.filter_items(config, items)  # type: ignore[arg-type]

    assert selection.selection == "full"
    assert selection.reason == "subset_matched_no_collected_test"
    assert [item.nodeid for item in items] == ["tests/a.py::test_fine"]
    assert config.hook.deselected == []


@responses.activate
def test_full_response_leaves_the_collection_untouched() -> None:
    # The engine serves a polymorphic response: `tests` is absent on `full`.
    responses.add(
        responses.GET,
        ENDPOINT,
        json={"selection": "full", "reason": "no_predecessor"},
    )
    selection = _make_selection()

    items = [FakeItem("tests/a.py::test_fine")]
    config = FakeConfig()
    selection.filter_items(config, items)  # type: ignore[arg-type]

    assert selection.selection == "full"
    assert selection.reason == "no_predecessor"
    assert len(items) == 1
    assert config.hook.deselected == []


@responses.activate
def test_full_response_from_an_older_engine_is_not_an_error() -> None:
    """An engine predating the polymorphic response still sends `tests: []`.

    The plugin and the engine ship independently, so both shapes are live at
    once. The old one must stay a plain `full` answer — not the API-error path,
    which would print a scary (and false) warning on every run.
    """
    responses.add(
        responses.GET,
        ENDPOINT,
        json={"selection": "full", "reason": "no_predecessor", "tests": []},
    )
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.reason == "no_predecessor"
    assert selection.init_error_msg is None


@responses.activate
def test_subset_without_tests_is_surfaced_as_an_error() -> None:
    """A `subset` answer must carry `tests`; if it does not, say so.

    The full suite runs either way, so this costs nothing to get wrong quietly
    -- which is exactly why it is worth a test. `tests` is only optional on a
    `full` answer; missing it on a subset means the engine broke its own
    contract, and a plugin that shrugged at that would hide a real bug behind a
    normal-looking run.
    """
    responses.add(
        responses.GET,
        ENDPOINT,
        json={"selection": "subset", "reason": "reduced_rerun"},
    )
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.init_error_msg is not None


@responses.activate
def test_http_error_degrades_to_full() -> None:
    responses.add(responses.GET, ENDPOINT, status=500)
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.init_error_msg is not None


@responses.activate
def test_missing_endpoint_degrades_silently_to_full() -> None:
    responses.add(responses.GET, ENDPOINT, status=404)
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.init_error_msg is None


@responses.activate
def test_connection_error_degrades_to_full() -> None:
    # No responses registered → ConnectionError raised by the responses lib.
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.init_error_msg is not None


@responses.activate
def test_non_object_payload_degrades_to_full() -> None:
    # Valid JSON but not an object: subscript access raises TypeError,
    # which must degrade to the full suite instead of crashing startup.
    responses.add(responses.GET, ENDPOINT, json=["unexpected", "shape"])
    selection = _make_selection()

    assert selection.selection == "full"
    assert selection.init_error_msg is not None
