import pytest

import _pytest.pytester
import _pytest.config

from pytest_mergify import utils


def test_span_resources_attributes(
    pytestconfig: _pytest.config.Config,
) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    assert plugin.exporter is not None
    spans = plugin.exporter.get_finished_spans()
    assert spans[0].resource.attributes["cicd.provider.name"] == utils.get_ci_provider()


@pytest.mark.skipif(
    utils.get_ci_provider() != "github_actions", reason="This test only supports GHA"
)
def test_span_github_actions(
    pytestconfig: _pytest.config.Config,
) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    assert plugin.exporter is not None
    spans = plugin.exporter.get_finished_spans()
    assert (
        spans[0].resource.attributes["vcs.repository.name"]
        == "Mergifyio/pytest-mergify"
    )
