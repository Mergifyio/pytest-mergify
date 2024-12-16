import _pytest.pytester
import _pytest.config


def test_span(
    pytestconfig: _pytest.config.Config,
) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    assert plugin.exporter is not None
    spans = plugin.exporter.get_finished_spans()
    assert any(s.name == "pytestconfig setup" for s in spans)
