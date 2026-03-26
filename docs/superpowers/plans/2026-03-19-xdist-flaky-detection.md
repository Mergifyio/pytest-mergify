# pytest-xdist Flaky Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full flaky detection support under pytest-xdist using controller-orchestrated pre-computed deadlines and xdist's built-in IPC.

**Architecture:** The controller fetches the flaky detection context from the API once, distributes it to workers via `workerinput`. Each worker independently constructs a `FlakyDetector`, computes the same global budget, runs tests with reruns, and sends metrics back via `workeroutput`. The controller aggregates metrics and generates the terminal report.

**Tech Stack:** Python 3.8+, pytest, pytest-xdist, OpenTelemetry, dataclasses

**Spec:** `docs/superpowers/specs/2026-03-19-xdist-flaky-detection-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `pytest_mergify/flaky_detection.py` | Modify | Add `from_context()` classmethod, `to_serializable_metrics()`, static deadline mode, `make_report_from_aggregated()` |
| `pytest_mergify/__init__.py` | Modify | Add xdist controller hooks (`pytest_configure_node`, `pytest_testnodedown`), worker init from `workerinput`, metrics export to `workeroutput` |
| `pytest_mergify/ci_insights.py` | Modify | Add `load_flaky_detector_from_context()` for worker-side construction |
| `tests/test_flaky_detection.py` | Modify | Add unit tests for `from_context()`, static deadline, `to_serializable_metrics()`, `make_report_from_aggregated()` |
| `tests/test_xdist.py` | Create | Integration tests for xdist flaky detection end-to-end |
| `tests/conftest.py` | Modify | Add xdist-aware test helper if needed |

---

### Task 1: Add `from_context()` classmethod to `FlakyDetector`

**Files:**
- Modify: `pytest_mergify/flaky_detection.py:108-163`
- Test: `tests/test_flaky_detection.py`

- [ ] **Step 1: Write the failing test for `from_context()`**

In `tests/test_flaky_detection.py`, add:

```python
def test_flaky_detector_from_context() -> None:
    context_dict = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_foo", "test_bar"],
        "existing_tests_mean_duration_ms": 5000,
        "unhealthy_test_names": ["test_foo"],
        "max_test_execution_count": 100,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }

    detector = flaky_detection.FlakyDetector.from_context(
        context_dict=context_dict,
        mode="new",
    )

    assert detector.mode == "new"
    assert detector._context.existing_test_names == ["test_foo", "test_bar"]
    assert detector._context.existing_tests_mean_duration_ms == 5000
    assert detector._context.max_test_execution_count == 100
    assert detector._test_metrics == {}
    assert detector._tests_to_process == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_from_context -v`
Expected: FAIL with `AttributeError: type object 'FlakyDetector' has no attribute 'from_context'`

- [ ] **Step 3: Implement `from_context()` classmethod**

In `pytest_mergify/flaky_detection.py`, add this classmethod to `FlakyDetector`:

```python
@classmethod
def from_context(
    cls,
    context_dict: typing.Dict[str, typing.Any],
    mode: typing.Literal["new", "unhealthy"],
) -> "FlakyDetector":
    """Construct from serialized context dict, skipping the API call."""
    instance = cls.__new__(cls)
    instance.token = ""
    instance.url = ""
    instance.full_repository_name = ""
    instance.mode = mode
    instance._context = _FlakyDetectionContext(**context_dict)
    instance._test_metrics = {}
    instance._over_length_tests = set()
    instance._available_budget_duration = datetime.timedelta()
    instance._tests_to_process = []
    instance._suspended_item_finalizers = {}
    instance._debug_logs = []
    return instance
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_from_context -v`
Expected: PASS

- [ ] **Step 5: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pytest_mergify/flaky_detection.py tests/test_flaky_detection.py
git commit -m "feat(flaky-detection): Add from_context() classmethod for xdist workers

Fixes: MRGFY-6296"
```

---

### Task 2: Add metrics serialization to `FlakyDetector`

**Files:**
- Modify: `pytest_mergify/flaky_detection.py`
- Test: `tests/test_flaky_detection.py`

- [ ] **Step 1: Write the failing test for `to_serializable_metrics()`**

In `tests/test_flaky_detection.py`, add:

```python
def test_flaky_detector_to_serializable_metrics() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(max_test_name_length=100)
    detector._test_metrics = {
        "test_foo": flaky_detection._TestMetrics(
            initial_setup_duration=datetime.timedelta(milliseconds=100),
            initial_call_duration=datetime.timedelta(milliseconds=200),
            initial_teardown_duration=datetime.timedelta(milliseconds=50),
            rerun_count=3,
            prevented_timeout=True,
            total_duration=datetime.timedelta(milliseconds=1050),
        ),
    }
    detector._over_length_tests = {"test_long_name"}
    detector._debug_logs = [
        utils.StructuredLog.make(message="test log", key="value"),
    ]

    result = detector.to_serializable_metrics()

    assert result["test_metrics"]["test_foo"]["rerun_count"] == 3
    assert result["test_metrics"]["test_foo"]["total_duration_ms"] == 1050.0
    assert result["test_metrics"]["test_foo"]["initial_setup_duration_ms"] == 100.0
    assert result["test_metrics"]["test_foo"]["initial_call_duration_ms"] == 200.0
    assert result["test_metrics"]["test_foo"]["initial_teardown_duration_ms"] == 50.0
    assert result["test_metrics"]["test_foo"]["prevented_timeout"] is True
    assert result["over_length_tests"] == ["test_long_name"]
    assert len(result["debug_logs"]) == 1
    assert result["debug_logs"][0]["message"] == "test log"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_to_serializable_metrics -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement `to_serializable_metrics()`**

In `pytest_mergify/flaky_detection.py`, add to `FlakyDetector`:

```python
def to_serializable_metrics(self) -> typing.Dict[str, typing.Any]:
    """Serialize metrics for transport via xdist workeroutput."""
    return {
        "test_metrics": {
            test: {
                "rerun_count": metrics.rerun_count,
                "total_duration_ms": metrics.total_duration.total_seconds() * 1000,
                "initial_setup_duration_ms": metrics.initial_setup_duration.total_seconds() * 1000,
                "initial_call_duration_ms": metrics.initial_call_duration.total_seconds() * 1000,
                "initial_teardown_duration_ms": metrics.initial_teardown_duration.total_seconds() * 1000,
                "prevented_timeout": metrics.prevented_timeout,
            }
            for test, metrics in self._test_metrics.items()
        },
        "over_length_tests": list(self._over_length_tests),
        "debug_logs": [
            {
                "timestamp": log.timestamp.isoformat(),
                "message": log.message,
                **log.attributes,
            }
            for log in self._debug_logs
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_to_serializable_metrics -v`
Expected: PASS

- [ ] **Step 5: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pytest_mergify/flaky_detection.py tests/test_flaky_detection.py
git commit -m "feat(flaky-detection): Add metrics serialization for xdist IPC

Fixes: MRGFY-6296"
```

---

### Task 3: Add `make_report_from_aggregated()` standalone function

**Files:**
- Modify: `pytest_mergify/flaky_detection.py`
- Test: `tests/test_flaky_detection.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_flaky_detection.py`, add:

```python
def test_make_report_from_aggregated() -> None:
    context_dict = {
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
    # Budget: max(0.1 * 10s, 4s) = 4s.
    metrics = {
        "test_metrics": {
            "test_bar": {
                "rerun_count": 10,
                "total_duration_ms": 1000.0,
                "initial_setup_duration_ms": 10.0,
                "initial_call_duration_ms": 80.0,
                "initial_teardown_duration_ms": 10.0,
                "prevented_timeout": False,
            },
        },
        "over_length_tests": [],
        "debug_logs": [],
    }

    report = flaky_detection.make_report_from_aggregated(
        context_dict=context_dict,
        mode="new",
        available_budget_duration_ms=4000.0,
        aggregated_metrics=metrics,
    )

    assert "Flaky detection" in report
    assert "test_bar" in report
    assert "has been tested 10 time" in report
    assert "Active for 1 new test" in report


def test_make_report_from_aggregated_no_tests() -> None:
    context_dict = {
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

    report = flaky_detection.make_report_from_aggregated(
        context_dict=context_dict,
        mode="new",
        available_budget_duration_ms=4000.0,
        aggregated_metrics={
            "test_metrics": {},
            "over_length_tests": [],
            "debug_logs": [],
        },
    )

    assert "No new tests detected" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flaky_detection.py::test_make_report_from_aggregated tests/test_flaky_detection.py::test_make_report_from_aggregated_no_tests -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement `make_report_from_aggregated()`**

Add as a module-level function in `pytest_mergify/flaky_detection.py`:

```python
def make_report_from_aggregated(
    context_dict: typing.Dict[str, typing.Any],
    mode: typing.Literal["new", "unhealthy"],
    available_budget_duration_ms: float,
    aggregated_metrics: typing.Dict[str, typing.Any],
) -> str:
    """Generate report on the controller from aggregated worker metrics."""
    context = _FlakyDetectionContext(**context_dict)
    test_metrics = aggregated_metrics["test_metrics"]
    over_length_tests = aggregated_metrics["over_length_tests"]
    debug_logs = aggregated_metrics["debug_logs"]

    result = "🐛 Flaky detection"

    if over_length_tests:
        result += (
            f"{os.linesep}- Skipped {len(over_length_tests)} "
            f"test{'s' if len(over_length_tests) > 1 else ''}:"
        )
        for test in over_length_tests:
            result += (
                f"{os.linesep}    • '{test}' has not been tested multiple times because the name of the test "
                f"exceeds our limit of {context.max_test_name_length} characters"
            )

    if not test_metrics:
        result += (
            f"{os.linesep}- No {mode} tests detected, but we are watching 👀"
        )
        return result

    available_budget_seconds = available_budget_duration_ms / 1000
    used_budget_ms = sum(
        m["total_duration_ms"] for m in test_metrics.values()
    )
    used_budget_seconds = used_budget_ms / 1000
    result += (
        f"{os.linesep}- Used {used_budget_seconds / available_budget_seconds * 100:.2f} % of the budget "
        f"({used_budget_seconds:.2f} s/{available_budget_seconds:.2f} s)"
    )

    result += (
        f"{os.linesep}- Active for {len(test_metrics)} {mode} "
        f"test{'s' if len(test_metrics) > 1 else ''}:"
    )
    for test, m in test_metrics.items():
        if m["rerun_count"] < context.min_test_execution_count:
            result += (
                f"{os.linesep}    • '{test}' is too slow to be tested at least "
                f"{context.min_test_execution_count} times within the budget"
            )
            continue

        rerun_duration_seconds = m["total_duration_ms"] / 1000
        result += (
            f"{os.linesep}    • '{test}' has been tested {m['rerun_count']} "
            f"time{'s' if m['rerun_count'] > 1 else ''} using approx. "
            f"{rerun_duration_seconds / available_budget_seconds * 100:.2f} % of the budget "
            f"({rerun_duration_seconds:.2f} s/{available_budget_seconds:.2f} s)"
        )

    tests_prevented_from_timeout = [
        test for test, m in test_metrics.items() if m["prevented_timeout"]
    ]
    if tests_prevented_from_timeout:
        result += (
            f"{os.linesep}⚠️ Reduced reruns for the following "
            f"test{'s' if len(tests_prevented_from_timeout) else ''} to respect 'pytest-timeout':"
        )
        for test in tests_prevented_from_timeout:
            result += f"{os.linesep}    • '{test}'"

        result += (
            f"{os.linesep}To improve flaky detection and prevent fixture-level timeouts from limiting reruns, enable function-only timeouts. "
            f"Reference: https://github.com/pytest-dev/pytest-timeout?tab=readme-ov-file#avoiding-timeouts-in-fixtures"
        )

    if os.environ.get("PYTEST_MERGIFY_DEBUG") and debug_logs:
        result += f"{os.linesep}🔎 Debug Logs"
        for log in debug_logs:
            result += f"{os.linesep}{json.dumps(log)}"

    return result
```

Note: Add `import json` at the top of `flaky_detection.py` if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flaky_detection.py::test_make_report_from_aggregated tests/test_flaky_detection.py::test_make_report_from_aggregated_no_tests -v`
Expected: PASS

- [ ] **Step 5: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pytest_mergify/flaky_detection.py tests/test_flaky_detection.py
git commit -m "feat(flaky-detection): Add make_report_from_aggregated() for xdist controller

Fixes: MRGFY-6296"
```

---

### Task 4: Add static deadline mode for xdist

**Files:**
- Modify: `pytest_mergify/flaky_detection.py:108-130,368-409`
- Test: `tests/test_flaky_detection.py`

- [ ] **Step 1: Write the failing test for static deadline**

In `tests/test_flaky_detection.py`, add:

```python
@freezegun.freeze_time(time_to_freeze=_NOW)
def test_flaky_detector_set_test_deadline_static() -> None:
    """Under xdist, deadlines use static per-test budget allocation."""
    detector = InitializedFlakyDetector()
    detector.mode = "new"
    detector._is_xdist = True
    detector._context = _make_flaky_detection_context()
    detector._available_budget_duration = datetime.timedelta(seconds=10)
    detector._tests_to_process = ["foo", "bar"]
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(),
    }

    detector.set_test_deadline(test="foo")

    metrics = detector._test_metrics["foo"]
    assert metrics.deadline is not None
    # Static: 10s / 2 tests = 5s per test.
    expected_deadline = _NOW + datetime.timedelta(seconds=5)
    assert metrics.deadline == expected_deadline
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_set_test_deadline_static -v`
Expected: FAIL with `AttributeError: '_is_xdist'`

- [ ] **Step 3: Add `_is_xdist` field and branch in `set_test_deadline`**

In `pytest_mergify/flaky_detection.py`, add to `FlakyDetector` dataclass fields (after `_debug_logs`):

```python
_is_xdist: bool = dataclasses.field(init=False, default=False)
```

Update `set_test_deadline` to branch on `_is_xdist`:

```python
def set_test_deadline(
    self, test: str, timeout: typing.Optional[datetime.timedelta] = None
) -> None:
    metrics = self._test_metrics[test]

    if self._is_xdist:
        # Static allocation: equal share of total budget per test.
        per_test_budget = self._available_budget_duration / max(
            len(self._tests_to_process), 1
        )
        metrics.deadline = datetime.datetime.now(datetime.timezone.utc) + per_test_budget
    else:
        remaining_budget = self._get_remaining_budget_duration()
        remaining_tests = self._count_remaining_tests()

        # Distribute remaining budget equally across remaining tests.
        metrics.deadline = datetime.datetime.now(datetime.timezone.utc) + (
            remaining_budget / remaining_tests
        )

    self._debug_logs.append(
        utils.StructuredLog.make(
            message="Deadline set",
            test=test,
            available_budget=str(self._available_budget_duration),
            remaining_budget=str(self._get_remaining_budget_duration()) if not self._is_xdist else "N/A (xdist)",
            is_xdist=self._is_xdist,
            all_tests=len(self._tests_to_process),
            remaining_tests=self._count_remaining_tests() if not self._is_xdist else "N/A (xdist)",
        )
    )

    if not timeout:
        return

    # Leave a margin of 10 %. Better safe than sorry. We don't want to crash
    # the CI.
    safe_timeout = timeout * 0.9
    timeout_deadline = datetime.datetime.now(datetime.timezone.utc) + safe_timeout
    if not metrics.deadline or timeout_deadline < metrics.deadline:
        metrics.deadline = timeout_deadline
        metrics.prevented_timeout = True
        self._debug_logs.append(
            utils.StructuredLog.make(
                message="Deadline updated to prevent timeout",
                test=test,
                timeout=str(timeout),
                safe_timeout=str(safe_timeout),
                deadline=metrics.deadline,
            )
        )
```

Also update `from_context()` to set `_is_xdist = True`.

Also update `InitializedFlakyDetector.__init__` in tests to add `self._is_xdist = False`. This is needed because `InitializedFlakyDetector` overrides `__init__` completely (bypassing the dataclass `__init__`), so dataclass field defaults like `_is_xdist: bool = dataclasses.field(init=False, default=False)` do NOT apply. Every field that the test code accesses must be manually initialized in `InitializedFlakyDetector.__init__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_flaky_detection.py::test_flaky_detector_set_test_deadline_static -v`
Expected: PASS

- [ ] **Step 5: Run all existing flaky detection tests to ensure no regression**

Run: `uv run pytest tests/test_flaky_detection.py tests/test_ci_insights.py -v`
Expected: All PASS

- [ ] **Step 6: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pytest_mergify/flaky_detection.py tests/test_flaky_detection.py
git commit -m "feat(flaky-detection): Add static deadline mode for xdist workers

Fixes: MRGFY-6296"
```

---

### Task 5: Add worker-side context loading to `MergifyCIInsights`

**Files:**
- Modify: `pytest_mergify/ci_insights.py:173-196`

- [ ] **Step 1: Add `load_flaky_detector_from_context()` to `MergifyCIInsights`**

In `pytest_mergify/ci_insights.py`, add:

```python
def load_flaky_detector_from_context(
    self,
    context_dict: typing.Dict[str, typing.Any],
    mode: str,
) -> None:
    """Construct FlakyDetector from pre-fetched context (xdist worker path)."""
    try:
        self.flaky_detector = flaky_detection.FlakyDetector.from_context(
            context_dict=context_dict,
            mode=mode,
        )
    except Exception as exception:
        self.flaky_detector_error_message = (
            f"Could not load flaky detector: {str(exception)}"
        )
```

- [ ] **Step 2: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/test_ci_insights.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add pytest_mergify/ci_insights.py
git commit -m "feat(ci-insights): Add worker-side flaky detector loading from context

Fixes: MRGFY-6296"
```

---

### Task 6: Add xdist controller hooks to `PytestMergify`

**Files:**
- Modify: `pytest_mergify/__init__.py`

This task adds the controller-side hooks: `pytest_configure_node` (distribute context) and `pytest_testnodedown` (collect metrics).

- [ ] **Step 1: Add xdist detection helper**

In `pytest_mergify/__init__.py`, add a helper function:

```python
def _is_xdist_controller(config: _pytest.config.Config) -> bool:
    """Check if running as xdist controller (not a worker)."""
    return (
        config.pluginmanager.has_plugin("dsession")
        and not hasattr(config, "workerinput")
    )


def _is_xdist_worker(config: _pytest.config.Config) -> bool:
    """Check if running as xdist worker."""
    return hasattr(config, "workerinput")
```

- [ ] **Step 2: Add controller state fields and `pytest_configure_node`**

In `PytestMergify` class, add:

```python
# xdist controller state.
_xdist_flaky_context: typing.Optional[typing.Dict[str, typing.Any]] = None
_xdist_flaky_mode: typing.Optional[str] = None
_xdist_aggregated_metrics: typing.Dict[str, typing.Any] = dataclasses.field(
    default_factory=lambda: {"test_metrics": {}, "over_length_tests": [], "debug_logs": []}
)
_xdist_available_budget_duration_ms: float = 0.0
```

Note: `PytestMergify` is a plain class, not a dataclass. Initialize these in `pytest_configure`:

Add `import dataclasses` at the top of `pytest_mergify/__init__.py`.

```python
def pytest_configure(self, config: _pytest.config.Config) -> None:
    kwargs = {}
    api_url = config.getoption("--mergify-api-url")
    if api_url is not None:
        kwargs["api_url"] = api_url
    self.mergify_ci = MergifyCIInsights(**kwargs)

    # xdist controller state.
    self._xdist_flaky_context: typing.Optional[typing.Dict[str, typing.Any]] = None
    self._xdist_flaky_mode: typing.Optional[str] = None
    self._xdist_aggregated_metrics: typing.Dict[str, typing.Any] = {
        "test_metrics": {}, "over_length_tests": [], "debug_logs": [],
    }
    self._xdist_available_budget_duration_ms: float = 0.0

    # On xdist controller, reuse the already-loaded detector's context
    # for distribution to workers. No extra API call needed since
    # MergifyCIInsights.__post_init__ already calls _load_flaky_detector().
    if _is_xdist_controller(config) and self.mergify_ci.flaky_detector:
        self._xdist_flaky_context = dataclasses.asdict(
            self.mergify_ci.flaky_detector._context
        )
        self._xdist_flaky_mode = self.mergify_ci.flaky_detector.mode
```

Add the xdist hook:

```python
def pytest_configure_node(self, node: typing.Any) -> None:
    """xdist hook: distribute flaky detection context to workers."""
    if self._xdist_flaky_context is not None:
        node.workerinput["flaky_detection_context"] = self._xdist_flaky_context
        node.workerinput["flaky_detection_mode"] = self._xdist_flaky_mode
```

- [ ] **Step 3: Add `pytest_testnodedown` to collect worker metrics**

```python
def pytest_testnodedown(self, node: typing.Any, error: typing.Any) -> None:
    """xdist hook: collect metrics from completed workers."""
    workeroutput = getattr(node, "workeroutput", None)
    if workeroutput is None:
        return

    worker_metrics = workeroutput.get("flaky_detection_metrics")
    if worker_metrics is None:
        return

    # Merge test metrics (workers run distinct tests, no overlap).
    self._xdist_aggregated_metrics["test_metrics"].update(
        worker_metrics.get("test_metrics", {})
    )
    self._xdist_aggregated_metrics["over_length_tests"].extend(
        worker_metrics.get("over_length_tests", [])
    )
    self._xdist_aggregated_metrics["debug_logs"].extend(
        worker_metrics.get("debug_logs", [])
    )

    if "available_budget_duration_ms" in worker_metrics:
        self._xdist_available_budget_duration_ms = worker_metrics[
            "available_budget_duration_ms"
        ]
```

- [ ] **Step 4: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 5: Run existing tests to ensure no regression**

Run: `uv run pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add pytest_mergify/__init__.py
git commit -m "feat(xdist): Add controller hooks for context distribution and metrics collection

Fixes: MRGFY-6296"
```

---

### Task 7: Add xdist worker initialization and metrics export

**Files:**
- Modify: `pytest_mergify/__init__.py`
- Modify: `pytest_mergify/flaky_detection.py`

This task makes workers read context from `workerinput` and export metrics via `workeroutput`.

- [ ] **Step 1: Update worker-side `pytest_configure` to load from `workerinput`**

Modify `PytestMergify.pytest_configure` to handle the xdist worker case. After the existing `MergifyCIInsights` initialization, add:

```python
# xdist worker: load flaky detector from controller-provided context.
if _is_xdist_worker(config):
    context = config.workerinput.get("flaky_detection_context")
    mode = config.workerinput.get("flaky_detection_mode")
    if context is not None and mode is not None:
        self.mergify_ci.load_flaky_detector_from_context(context, mode)
```

- [ ] **Step 2: Update `pytest_sessionfinish` to export metrics**

**Important:** The current `pytest_sessionfinish` is a `hookwrapper=True` generator that returns early when `self.tracer` is falsy. On xdist workers, the tracer may not be initialized (no token/repo in worker env). The metrics export must happen **outside** the tracer guard.

Restructure `pytest_sessionfinish` so the xdist worker export runs unconditionally:

```python
@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(
    self,
    session: _pytest.main.Session,
) -> typing.Generator[None, None, None]:
    # xdist worker: export metrics via workeroutput (before yield, independent of tracer).
    if _is_xdist_worker(session.config) and self.mergify_ci.flaky_detector:
        workeroutput = getattr(session.config, "workeroutput", None)
        if workeroutput is not None:
            metrics = self.mergify_ci.flaky_detector.to_serializable_metrics()
            metrics["available_budget_duration_ms"] = (
                self.mergify_ci.flaky_detector._available_budget_duration.total_seconds() * 1000
            )
            workeroutput["flaky_detection_metrics"] = metrics

    if not self.tracer:
        yield
        return

    yield

    self.session_span.set_status(
        opentelemetry.trace.StatusCode.ERROR
        if self.has_error
        else opentelemetry.trace.StatusCode.OK
    )
    self.session_span.end()
```

- [ ] **Step 3: Update `pytest_terminal_summary` to use aggregated metrics under xdist**

In the terminal summary method, update the flaky detection report section to handle xdist:

```python
if _is_xdist_controller(terminalreporter.config):
    if self._xdist_flaky_context:
        # Always show report (even if no test_metrics — shows "No new tests detected").
        from pytest_mergify import flaky_detection
        terminalreporter.write_line(
            flaky_detection.make_report_from_aggregated(
                context_dict=self._xdist_flaky_context,
                mode=self._xdist_flaky_mode or "new",
                available_budget_duration_ms=self._xdist_available_budget_duration_ms,
                aggregated_metrics=self._xdist_aggregated_metrics,
            )
        )
    elif self.mergify_ci.flaky_detector_error_message:
        terminalreporter.write_line(
            f"""⚠️ Flaky detection couldn't be enabled because of an error.

Common issues:
  • Your 'MERGIFY_TOKEN' might not be set or could be invalid
  • There might be a network connectivity issue with the Mergify API

📚 Documentation: https://docs.mergify.com/ci-insights/test-frameworks/pytest/
🔍 Details: {self.mergify_ci.flaky_detector_error_message}""",
            yellow=True,
        )
elif self.mergify_ci.flaky_detector:
    terminalreporter.write_line(self.mergify_ci.flaky_detector.make_report())
elif self.mergify_ci.flaky_detector_error_message:
    # ... existing error message code (unchanged) ...
```

**Note on controller-side flaky detector:** The controller's `MergifyCIInsights.__post_init__` already calls `_load_flaky_detector()` which fetches the context from the API. Task 6 reuses that already-loaded context via `dataclasses.asdict(self.mergify_ci.flaky_detector._context)`. No second API call is needed. On xdist workers, the normal `_load_flaky_detector()` path will be a no-op (no token/repo env vars in worker processes), and workers load via `load_flaky_detector_from_context()` from `workerinput` instead.

- [ ] **Step 4: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 5: Run existing tests**

Run: `uv run pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add pytest_mergify/__init__.py pytest_mergify/ci_insights.py
git commit -m "feat(xdist): Add worker initialization and metrics export

Fixes: MRGFY-6296"
```

---

### Task 8: Add xdist integration tests

**Files:**
- Create: `tests/test_xdist.py`
- Modify: `pyproject.toml` (add `pytest-xdist` to dev dependencies)

- [ ] **Step 1: Add `pytest-xdist` to dev dependencies**

In `pyproject.toml`, add to `[dependency-groups] dev`:

```
"pytest-xdist>=3.0",
```

Run: `uv sync`

- [ ] **Step 2: Write integration test for flaky detection under xdist (new mode)**

Create `tests/test_xdist.py`.

**Important:** Use `runpytest_inprocess` (not `runpytest`) because the `responses` library only intercepts HTTP calls in the current process. `runpytest` forks a subprocess where `responses` mocks won't be active. For xdist integration, we test by constructing the plugin with pre-loaded context (simulating the controller/worker handoff) rather than spawning actual xdist workers.

```python
import datetime
import re
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
def test_flaky_detection_xdist_new_tests(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    """Flaky detection works end-to-end with xdist-style context loading."""
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_xdist_new_tests.py::test_existing",
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
```

- [ ] **Step 3: Run the integration test**

Run: `uv run pytest tests/test_xdist.py::test_flaky_detection_xdist_new_tests -v`
Expected: PASS

- [ ] **Step 4: Write unit test for xdist-style `from_context` + `prepare_for_session` flow**

This tests the worker-side flow: constructing a detector from context dict, preparing it, and verifying static deadline behavior.

Add to `tests/test_xdist.py`:

```python
def test_xdist_worker_flow_from_context_to_metrics() -> None:
    """Simulate the full xdist worker lifecycle: context -> prepare -> serialize."""
    context_dict = {
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
```

- [ ] **Step 5: Run the unit test**

Run: `uv run pytest tests/test_xdist.py::test_xdist_worker_flow_from_context_to_metrics -v`
Expected: PASS

- [ ] **Step 6: Write test for `make_report_from_aggregated` with real aggregated data**

Add to `tests/test_xdist.py`:

```python
def test_xdist_aggregated_report() -> None:
    """Controller generates correct report from aggregated worker metrics."""
    context_dict = {
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
    worker1_metrics = {
        "test_new_a": {
            "rerun_count": 10,
            "total_duration_ms": 500.0,
            "initial_setup_duration_ms": 5.0,
            "initial_call_duration_ms": 40.0,
            "initial_teardown_duration_ms": 5.0,
            "prevented_timeout": False,
        },
    }
    worker2_metrics = {
        "test_new_b": {
            "rerun_count": 8,
            "total_duration_ms": 400.0,
            "initial_setup_duration_ms": 5.0,
            "initial_call_duration_ms": 45.0,
            "initial_teardown_duration_ms": 5.0,
            "prevented_timeout": False,
        },
    }

    aggregated = {
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
```

- [ ] **Step 8: Write edge case test: no xdist (normal operation unchanged)**

Add to `tests/test_xdist.py`:

```python
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
```

- [ ] **Step 9: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All PASS

- [ ] **Step 10: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add tests/test_xdist.py pyproject.toml uv.lock
git commit -m "test(xdist): Add integration tests for flaky detection under xdist

Fixes: MRGFY-6296"
```

---

### Task 9: Disable flaky detection under `each` scheduling mode

**Files:**
- Modify: `pytest_mergify/__init__.py`
- Test: `tests/test_xdist.py`

- [ ] **Step 1: Write the failing test**

This is a unit test for the `pytest_configure_node` guard — we test that the method does NOT inject context when `dist == "each"`. No subprocess needed.

Add to `tests/test_xdist.py`:

```python
def test_flaky_detection_disabled_under_each_mode() -> None:
    """Controller does not distribute flaky context under 'each' scheduling."""
    plugin = pytest_mergify.PytestMergify()
    plugin._xdist_flaky_context = {"existing_test_names": ["test_a"]}
    plugin._xdist_flaky_mode = "new"

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
    plugin._xdist_flaky_context = {"existing_test_names": ["test_a"]}
    plugin._xdist_flaky_mode = "new"

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_xdist.py::test_flaky_detection_disabled_under_each_mode tests/test_xdist.py::test_flaky_detection_enabled_under_load_mode -v`
Expected: FAIL (`pytest_configure_node` doesn't exist yet or doesn't check dist mode)

- [ ] **Step 3: Add `each` mode check in `pytest_configure_node`**

In `pytest_mergify/__init__.py`, update `pytest_configure_node`:

```python
def pytest_configure_node(self, node: typing.Any) -> None:
    """xdist hook: distribute flaky detection context to workers."""
    # Disable under 'each' mode to avoid duplicated budgets.
    dist_mode = getattr(node.config.option, "dist", None)
    if dist_mode == "each":
        return

    if self._xdist_flaky_context is not None:
        node.workerinput["flaky_detection_context"] = self._xdist_flaky_context
        node.workerinput["flaky_detection_mode"] = self._xdist_flaky_mode
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_xdist.py::test_flaky_detection_disabled_under_each_mode tests/test_xdist.py::test_flaky_detection_enabled_under_load_mode -v`
Expected: PASS

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pytest_mergify/__init__.py tests/test_xdist.py
git commit -m "feat(xdist): Disable flaky detection under 'each' scheduling mode

Fixes: MRGFY-6296"
```

---

### Task 10: Final regression and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All PASS

- [ ] **Step 2: Run linters**

Run: `uv run poe linters`
Expected: PASS

- [ ] **Step 3: Verify non-xdist tests still pass**

Run: `uv run pytest tests/test_ci_insights.py tests/test_flaky_detection.py tests/test_spans.py -v`
Expected: All PASS, no behavioral change

- [ ] **Step 4: Commit any cleanup**

```bash
git add -A
git commit -m "chore: Final cleanup for xdist flaky detection support

Fixes: MRGFY-6296"
```
