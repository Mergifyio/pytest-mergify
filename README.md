# pytest-mergify

Pytest plugin for [Mergify Test Insights](https://docs.mergify.com/ci-insights/).

More information at https://mergify.com

## Features

- **Test tracing** — Sends OpenTelemetry traces for every test to Mergify's API
- **Flaky test detection** — Intelligently reruns tests to detect flakiness with budget constraints
- **Test quarantine** — Quarantines failing tests so they don't block CI

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

For detailed documentation, see the [official guide](https://docs.mergify.com/ci-insights/test-frameworks/pytest/).

## Development

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
