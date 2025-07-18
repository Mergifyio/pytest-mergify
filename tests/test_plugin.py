import pytest

import _pytest.config
from _pytest.pytester import Pytester

import pytest_mergify
from . import conftest


def test_plugin_is_loaded(pytestconfig: _pytest.config.Config) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("pytest_mergify")
    assert plugin is pytest_mergify

    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert isinstance(plugin, pytest_mergify.PytestMergify)


def test_no_ci(pytester_with_spans: conftest.PytesterWithSpanT) -> None:
    result, spans = pytester_with_spans(setenv={"CI": "false"})
    assert spans is None
    assert all("Mergify" not in line for line in result.stdout.lines)


@pytest.mark.parametrize("env", ("PYTEST_MERGIFY_ENABLED", "CI"))
def test_enabled(
    env: str,
    pytester_with_spans: conftest.PytesterWithSpanT,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    result, spans = pytester_with_spans(setenv={env: "true"})
    assert spans is not None
    assert any("Mergify CI" in line for line in result.stdout.lines)


def test_empty_token(pytester_with_spans: conftest.PytesterWithSpanT) -> None:
    result, spans = pytester_with_spans(
        setenv={
            "MERGIFY_TOKEN": "",
            "_PYTEST_MERGIFY_TEST": None,
        }
    )
    assert spans is None
    assert (
        "No token configured for Mergify; test results will not be uploaded"
        in result.stdout.lines
    )


def test_no_token(pytester_with_spans: conftest.PytesterWithSpanT) -> None:
    result, spans = pytester_with_spans(
        setenv={
            "MERGIFY_TOKEN": None,
            "_PYTEST_MERGIFY_TEST": None,
        }
    )
    assert spans is None
    assert (
        "No token configured for Mergify; test results will not be uploaded"
        in result.stdout.lines
    )


@pytest.mark.parametrize("http_server", [200], indirect=True)
def test_with_token_gha(
    pytester: Pytester,
    monkeypatch: pytest.MonkeyPatch,
    http_server: str,
) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_TOKEN", "foobar")
    monkeypatch.setenv("MERGIFY_API_URL", http_server)
    pytester.makepyfile(
        """
        def test_foo():
            assert True
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    for line in result.stdout.lines:
        if line.startswith("MERGIFY_TEST_RUN_ID="):
            _, test_run_id = line.split("=", 2)
            assert len(test_run_id) == 16
            assert len(bytes.fromhex(test_run_id)) == 8
            break
    else:
        pytest.fail("No trace id found")


def test_repo_name_github_actions(
    pytester: Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    plugin = pytest_mergify.PytestMergify()
    pytester.makepyfile("")
    pytester.runpytest_inprocess(plugins=[plugin])
    assert plugin.mergify_tracer.repo_name == "Mergifyio/pytest-mergify"


def test_with_token_empty_repo(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans(
        setenv={
            "GITHUB_ACTIONS": "true",
            "MERGIFY_TOKEN": "x",
            "_PYTEST_MERGIFY_TEST": "false",
            "GITHUB_REPOSITORY": "",
        }
    )
    assert (
        "Unable to determine repository name; test results will not be uploaded"
        in result.stdout.lines
    )


def test_with_token_no_repo(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans(
        setenv={
            "GITHUB_ACTIONS": "true",
            "MERGIFY_TOKEN": "x",
            "_PYTEST_MERGIFY_TEST": "false",
            "GITHUB_REPOSITORY": None,
        }
    )
    assert (
        "Unable to determine repository name; test results will not be uploaded"
        in result.stdout.lines
    )


def test_errors_logs(
    pytester: _pytest.pytester.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This will try to upload traces, but we don't have a real exporter so it will log errors.
    monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)
    monkeypatch.setenv("MERGIFY_TOKEN", "x")
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "foo/bar")
    pytester.makepyfile(
        """
        def test_pass():
            pass
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert any(
        line.startswith(
            "Error while exporting traces: HTTPConnectionPool(host='localhost', port=9999): Max retries exceeded with url"
        )
        for line in result.stdout.lines
    )
    assert not any(
        line.startswith("::notice title=Mergify CI::MERGIFY_TEST_RUN_ID=")
        for line in result.stdout.lines
    )


@pytest.mark.parametrize("http_server", [403], indirect=True)
def test_errors_logs_403(
    pytester: _pytest.pytester.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    http_server: str,
) -> None:
    # This will try to upload traces, but we don't have a real exporter so it will log errors.
    monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)
    monkeypatch.setenv("MERGIFY_TOKEN", "x")
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "foo/bar")
    monkeypatch.setenv("MERGIFY_API_URL", http_server)
    pytester.makepyfile(
        """
        def test_pass():
            pass
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert any(
        line.startswith(
            "Error while exporting traces: 403 Client Error: Forbidden for url:"
        )
        for line in result.stdout.lines
    )
    assert not any(
        line.startswith("::notice title=Mergify CI::MERGIFY_TEST_RUN_ID=")
        for line in result.stdout.lines
    )
