"""Task 4.9 — the dashboard and alerts actually match what we export.

These files are YAML and JSON that nothing imports, so nothing would ever
tell you they had drifted. The specific way that goes wrong is quiet and
nasty: rename or drop a metric in metrics.py, and the Grafana panel keeps
rendering — as an empty graph. An empty graph and a healthy quiet system
look identical, which is precisely the failure this task exists to prevent.

So the check is mechanical: every `voiceagent_*` name used in an alert rule
or a dashboard panel must be one app/core/metrics.py really produces.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
METRICS_SOURCE = Path(__file__).resolve().parents[1] / "app" / "core" / "metrics.py"

_METRIC_NAME = re.compile(r"voiceagent_[a-z_]+")


def _exported_metric_names() -> set[str]:
    """The names actually registered as Gauges, read from the source rather
    than by importing and scraping — this must not depend on a database or
    Redis being reachable to run."""
    source = METRICS_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r'"(voiceagent_[a-z_]+)"', source))


def test_every_metric_the_alerts_use_is_one_we_export():
    exported = _exported_metric_names()
    rules = yaml.safe_load((DEPLOY / "alerts.yml").read_text(encoding="utf-8"))

    referenced = set()
    for group in rules["groups"]:
        for rule in group["rules"]:
            referenced |= set(_METRIC_NAME.findall(rule["expr"]))

    assert referenced, "the alert rules reference no metrics at all"
    assert not (referenced - exported), (
        f"alerts.yml watches metrics that are never exported: "
        f"{sorted(referenced - exported)} — those alerts can never fire"
    )


def test_every_metric_the_dashboard_uses_is_one_we_export():
    exported = _exported_metric_names()
    dashboard = json.loads(
        (DEPLOY / "grafana" / "dashboards" / "voiceagent.json").read_text(encoding="utf-8")
    )

    referenced = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            referenced |= set(_METRIC_NAME.findall(target.get("expr", "")))

    assert referenced, "the dashboard queries no metrics at all"
    assert not (referenced - exported), (
        f"the dashboard plots metrics that are never exported: "
        f"{sorted(referenced - exported)} — those panels render as empty "
        f"graphs, which looks exactly like a healthy quiet system"
    )


def test_the_dashboard_covers_everything_worth_watching():
    """The other direction. A metric added to metrics.py and then never put
    on the dashboard or an alert is one nobody will ever look at."""
    exported = _exported_metric_names()
    dashboard = (DEPLOY / "grafana" / "dashboards" / "voiceagent.json").read_text(encoding="utf-8")
    alerts = (DEPLOY / "alerts.yml").read_text(encoding="utf-8")
    watched = set(_METRIC_NAME.findall(dashboard)) | set(_METRIC_NAME.findall(alerts))

    unwatched = exported - watched
    assert not unwatched, (
        f"exported but on no panel and no alert: {sorted(unwatched)} — add it to "
        f"deploy/grafana/dashboards/voiceagent.json or deploy/alerts.yml, or stop "
        f"exporting it"
    )


@pytest.mark.parametrize(
    "path",
    [
        "prometheus.yml",
        "alerts.yml",
        "docker-compose.monitoring.yml",
        "grafana/provisioning/datasources/prometheus.yml",
        "grafana/provisioning/dashboards/dashboards.yml",
    ],
)
def test_the_monitoring_config_files_parse(path):
    """A YAML typo here surfaces as a container that will not start, on the
    server, at the moment somebody is trying to look at a dashboard during
    an incident."""
    loaded = yaml.safe_load((DEPLOY / path).read_text(encoding="utf-8"))
    assert loaded, f"{path} parsed as empty"


def test_neither_monitoring_port_is_published_publicly():
    """Grafana ships with a default admin password and this Prometheus has
    no authentication at all. Both are bound to 127.0.0.1 so reaching them
    needs an SSH tunnel — this is the one setup step where getting it wrong
    is completely silent."""
    compose = yaml.safe_load(
        (DEPLOY / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
    )

    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} publishes {mapping} on every interface — that puts "
                f"{'Grafana with its default password' if name == 'grafana' else 'an unauthenticated Prometheus'} "
                f"on a public IP"
            )


def test_the_scrape_config_still_sends_a_credential():
    """/metrics refuses anonymous requests (see test_phase4_disclosure.py).
    A scrape config that forgot the token would leave every panel empty,
    which reads as a quiet system rather than as a broken one."""
    scrape = yaml.safe_load((DEPLOY / "prometheus.yml").read_text(encoding="utf-8"))
    job = scrape["scrape_configs"][0]

    assert job["authorization"]["type"] == "Bearer"
    assert job["authorization"]["credentials"], "no bearer token configured for the scrape"
