# pytest-mergify

> **This repository is no longer the development home of `pytest-mergify`.**
> Development continues in [Mergifyio/mergify-ci-integrations](https://github.com/Mergifyio/mergify-ci-integrations),
> under [`clients/pytest-mergify/`](https://github.com/Mergifyio/mergify-ci-integrations/tree/main/clients/pytest-mergify).
> Please open issues and pull requests there.
>
> **The package itself is unaffected.** `pytest-mergify` is still maintained and
> still published to PyPI — just from the monorepo now. `pip install pytest-mergify`
> keeps working, and the installation instructions below are unchanged.

Pytest plugin for [Mergify Test Insights](https://docs.mergify.com/ci-insights/).

More information at https://mergify.com

## Features

- **Test tracing** — Sends OpenTelemetry traces for every test to Mergify's API
- **Flaky test detection** — Intelligently reruns tests to detect flakiness with budget constraints
- **Test quarantine** — Quarantines failing tests so they don't block CI
- **Test selection** — Runs only the previously-failing tests when Mergify's merge queue reruns a job

## Installation

Install the package alongside `pytest` (>= 6.0.0):

```bash
pip install pytest-mergify
```

The plugin is auto-discovered by pytest — no manual registration required.

## Configuration

Set the `MERGIFY_TOKEN` environment variable with your Mergify API token.

The plugin activates automatically when running in CI (detected via the `CI` environment variable). To enable outside CI, set `PYTEST_MERGIFY_ENABLE=true`.

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `MERGIFY_TOKEN` | Mergify API authentication token | (required) |
| `MERGIFY_API_URL` | Mergify API endpoint | `https://api.mergify.com` |
| `PYTEST_MERGIFY_ENABLE` | Force-enable outside CI | `false` |
| `PYTEST_MERGIFY_DEBUG` | Print spans to console | `false` |
| `MERGIFY_TRACEPARENT` | W3C distributed trace context | — |
| `MERGIFY_TEST_JOB_NAME` | Mergify test job name | — |
| `MERGIFY_TEST_SELECTION_DISABLE` | Opt out of test selection (see below) | `false` |

For detailed documentation, see the [official guide](https://docs.mergify.com/ci-insights/test-frameworks/pytest/).

### Test selection

When Mergify's merge queue reruns a job — a retry, or a step of a batch
bisection — only the tests that failed on the previous attempt are
informative. The plugin asks Mergify whether the current run is such a rerun
and, if so, runs only those tests; the rest are reported as deselected.

Nothing to configure: the plugin uses the token and job identity it already
has, and Mergify decides. Any other situation — a normal run, a rerun Mergify
has no previous results for, an unreachable API — runs the full suite, so the
feature can only remove work, never coverage. The feature is also enabled per
organization on Mergify's side, so it stays inactive until your organization
is opted in.

Set `MERGIFY_TEST_SELECTION_DISABLE=true` in your CI to opt out: the plugin
then always runs the full suite and never queries the endpoint. It is scoped
to this feature only — test tracing, flaky detection and quarantine keep
working (unset `MERGIFY_TOKEN` to turn the plugin off entirely).

## Development

Development happens in [`clients/pytest-mergify/`](https://github.com/Mergifyio/mergify-ci-integrations/tree/main/clients/pytest-mergify)
of the [mergify-ci-integrations](https://github.com/Mergifyio/mergify-ci-integrations)
monorepo; the instructions below are kept for reference only.

### Prerequisites

- Python >= 3.8
- [uv](https://docs.astral.sh/uv/)

### Setup

```bash
uv sync
```

### Running Tests

```bash
uv run poe test
```

### Linting

```bash
uv run poe linters
```

## License

GPL-3.0-only
