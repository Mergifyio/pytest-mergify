# Add pytest-xdist Support to Flaky Detection

**Linear:** MRGFY-6296
**Status:** Approved
**Date:** 2026-03-19

## Problem

The flaky detection system does not support `pytest-xdist`:

1. `flaky_detector._test_metrics` lives in-process memory, but xdist spawns separate worker processes.
2. `pytest_collection_finish` does not run on the controller under xdist.

## Decision Summary

- **Approach:** Controller-orchestrated with pre-computed per-test deadlines.
- **IPC:** xdist built-in `workerinput`/`workeroutput`.
- **Budget model:** Global budget, static per-test allocation under xdist. Dynamic deadlines preserved for non-xdist.
- **Scheduling:** Target `load` (default) mode. Other modes should not crash. Under `each` mode (every test runs on every worker), flaky detection is disabled to avoid duplicated budgets.

## Architecture

```
Controller                          Workers (gw0, gw1, ...)
────────────────────────────────    ────────────────────────────────
fetch flaky context from API
    │
    ├─── workerinput ──────────►    receive context as plain dict
    │                               build FlakyDetector (no API call)
    │                               collect tests (same list)
    │                               compute budget (same result)
    │                               run tests + reruns
    │                               ◄── workeroutput ───────────┤
aggregate metrics
print terminal summary
```

All workers collect the same full test list (xdist verifies this). Budget computation is deterministic, so each worker independently arrives at the same global budget and per-test allocation. No mid-run coordination.

## Controller Responsibilities

### 1. Fetch context and distribute (`pytest_configure_node`)

- Fetch `_FlakyDetectionContext` from API **once** (cache it).
- Serialize as plain dict into `node.workerinput["flaky_detection_context"]`.
- Also set `node.workerinput["flaky_detection_mode"]`.

### 2. Collect worker metrics (`pytest_testnodedown`)

- Read `node.workeroutput["flaky_detection_metrics"]`.
- Merge into controller-side aggregated metrics dict.
- Workers run distinct tests under `load` scheduling, so no overlap.

### 3. Terminal summary (`pytest_terminal_summary`)

- Build report from aggregated metrics using same format as today.

## Worker Responsibilities

### 1. Initialization

- Read `config.workerinput["flaky_detection_context"]` if present.
- Construct `FlakyDetector` via new `from_context()` classmethod (skips API call).

### 2. Session preparation (`pytest_collection_finish`)

- Call `prepare_for_session(session)` as today.

### 3. Test execution (`pytest_runtest_protocol`)

- Identical to current logic: initial run, set deadline, rerun loop.
- `set_test_deadline` uses static allocation: `total_budget / global_num_tests_to_process` where the denominator is the **global** count of tests to process (computed from the full collection, not from the worker's assigned subset). Workers don't know upfront which tests they'll run (xdist dispatches dynamically), but the per-test budget is the same regardless.

### 4. Metrics export (`pytest_sessionfinish`)

- Serialize `_test_metrics`, `_over_length_tests`, `_debug_logs` into `config.workeroutput["flaky_detection_metrics"]`.

## Data Flow

### workerinput (controller -> worker)

```python
node.workerinput["flaky_detection_context"] = {
    "budget_ratio_for_new_tests": float,
    "budget_ratio_for_unhealthy_tests": float,
    "existing_test_names": list[str],
    "existing_tests_mean_duration_ms": int,
    "unhealthy_test_names": list[str],
    "max_test_execution_count": int,
    "max_test_name_length": int,
    "min_budget_duration_ms": int,
    "min_test_execution_count": int,
}
node.workerinput["flaky_detection_mode"] = "new" | "unhealthy"
```

### workeroutput (worker -> controller)

```python
config.workeroutput["flaky_detection_metrics"] = {
    "test_metrics": {
        "tests/test_foo.py::test_bar": {
            "rerun_count": int,
            "total_duration_ms": float,
            "initial_setup_duration_ms": float,
            "initial_call_duration_ms": float,
            "initial_teardown_duration_ms": float,
            "prevented_timeout": bool,
        },
    },
    "over_length_tests": list[str],
    "debug_logs": list[dict],
}
```

The three initial duration sub-fields are needed because `make_report` uses `initial_duration` (their sum) and `is_test_too_slow` compares it against remaining time. Serializing them separately preserves full fidelity.

## FlakyDetector Changes

### New classmethod

`FlakyDetector.from_context(context_dict, mode)` is a `@classmethod` that constructs a `FlakyDetector` from a serialized context dict, skipping `_fetch_context()`. It sets `token`, `url`, and `full_repository_name` to empty strings (the dataclass fields remain required, but these values are unused on workers). The `_context` field is populated directly from the dict.

On the controller side, `FlakyDetector` is **not** instantiated. The controller only holds the raw context dict (for `workerinput`) and aggregated metrics (from `workeroutput`). The report is generated via `make_report_from_aggregated`, which is a standalone function that operates on plain dicts.

### Deadline computation

- **Non-xdist (unchanged):** Dynamic `remaining_budget / remaining_tests`.
- **xdist:** Static `total_budget / num_tests_to_process`.

Branch via a single `if` in `set_test_deadline`.

### Report from aggregated data

`make_report_from_aggregated(context, mode, metrics, over_length_tests, debug_logs)` runs on the controller from deserialized worker data.

## Error Handling

- **Worker crash:** `workeroutput` may be missing. Controller skips that worker's data and shows partial report.
- **Context fetch fails:** No context sent to workers, workers skip flaky detection. Same as today.
- **No context in workerinput:** Worker skips flaky detection gracefully.

## Testing Strategy

### Unit tests

- `from_context()` construction from plain dict.
- Static deadline computation.
- `make_report_from_aggregated()` output from deserialized metrics.

### Integration tests

- `pytester` with `-n 2`: end-to-end flaky detection under xdist.
- Metrics aggregation across workers (check terminal summary).
- Budget respected across workers.

### Edge cases

- Single worker (`-n 1`).
- Worker crash: partial report, no controller crash.
- No tests to process.
- xdist not installed: no import errors.

### Regression

All existing non-xdist tests must keep passing unchanged.
