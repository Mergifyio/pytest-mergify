import re

import _pytest.pytester

from tests import conftest


def test_a_run_uploads_its_spans(
    pytester: _pytest.pytester.Pytester,
    uploading_collector: conftest.OTLPCollector,
) -> None:
    pytester.makepyfile("def test_pass(): pass")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    assert len(uploading_collector.batches) == 1
    assert uploading_collector.span_names == {
        "pytest session start",
        "test_a_run_uploads_its_spans.py::test_pass",
    }


def test_an_uploaded_span_carries_its_attributes(
    pytester: _pytest.pytester.Pytester,
    uploading_collector: conftest.OTLPCollector,
) -> None:
    # Asserting on the decoded payload rather than on terminal text: a run can
    # print a run id and still have uploaded nothing.
    pytester.makepyfile("def test_pass(): pass")

    result = pytester.runpytest_subprocess()

    # Asserted before the payload, so a run that died on the way to uploading
    # reads as the failure it is rather than as a missing key.
    result.assert_outcomes(passed=1)
    (batch,) = uploading_collector.batches
    span = batch.span("test_an_uploaded_span_carries_its_attributes.py::test_pass")

    assert span.attributes["test.case.result.status"] == "passed"
    assert span.attributes["test.scope"] == "case"
    assert (
        batch.resource_attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
    )
    # The id the run reported to the user has to be the one it filed the spans
    # under, or the summary sends them looking up somebody else's run.
    printed_run_id = re.search(r"MERGIFY_TEST_RUN_ID=(\w+)", result.stdout.str())
    assert printed_run_id is not None
    assert batch.resource_attributes["test.run.id"] == printed_run_id.group(1)


def test_a_distributed_run_uploads_each_test_once(
    pytester: _pytest.pytester.Pytester,
    uploading_collector: conftest.OTLPCollector,
) -> None:
    """
    Under xdist every worker uploads on its own, from its own process.

    Nothing the controller reports can show a test whose span never left the
    worker that ran it, nor one that two of them each sent home, so the wire is
    the only place the split is observable.
    """
    pytester.makepyfile(
        """
        def test_a(): pass
        def test_b(): pass
        def test_c(): pass
        def test_d(): pass
        """
    )

    result = pytester.runpytest_subprocess("-n", "2")

    result.assert_outcomes(passed=4)
    uploaded = [
        span.name
        for batch in uploading_collector.batches
        for span in batch.spans
        if span.name != "pytest session start"
    ]
    assert sorted(uploaded) == [
        f"test_a_distributed_run_uploads_each_test_once.py::test_{name}"
        for name in ("a", "b", "c", "d")
    ]
