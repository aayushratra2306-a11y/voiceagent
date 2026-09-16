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
        "alertmanager.yml",
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


# ---------------------------------------------------------------------------
# Alert routing — getting them to a person
# ---------------------------------------------------------------------------
#
# The rules in alerts.yml have always fired; they fired into Prometheus's own
# screen, which nobody sits watching. Everything below is about the step after
# that, and every failure it checks for is silent: the alert still "fires",
# and still reaches nobody.


def _alertmanager_config() -> dict:
    return yaml.safe_load((DEPLOY / "alertmanager.yml").read_text(encoding="utf-8"))


def test_prometheus_knows_where_to_send_its_alerts():
    """Without this block Prometheus evaluates every rule, shows them firing
    on its own page, and tells nobody — which is indistinguishable from
    working, right up until the outage nobody hears about."""
    scrape = yaml.safe_load((DEPLOY / "prometheus.yml").read_text(encoding="utf-8"))

    targets = [
        target
        for entry in scrape["alerting"]["alertmanagers"]
        for config in entry["static_configs"]
        for target in config["targets"]
    ]

    assert targets, "Prometheus has no alertmanager configured to send alerts to"
    assert any("alertmanager" in t for t in targets), (
        f"the alertmanager target does not name the alertmanager service: {targets}"
    )


def test_every_route_leads_to_a_receiver_that_exists():
    """The nastiest misconfiguration in this file: a route naming a receiver
    that was renamed or never written. Alertmanager accepts the alert, matches
    the route, finds nothing to deliver to, and drops it in silence."""
    config = _alertmanager_config()
    defined = {r["name"] for r in config["receivers"]}

    used = {config["route"]["receiver"]}
    for child in config["route"].get("routes", []):
        used.add(child["receiver"])

    assert not (used - defined), (
        f"routed to receivers that do not exist: {sorted(used - defined)} — "
        f"alerts matching those routes are accepted and then silently dropped"
    )


def test_at_least_one_receiver_actually_delivers_somewhere():
    """A receiver with no delivery method configured is valid YAML, valid
    Alertmanager config, and a black hole."""
    config = _alertmanager_config()

    delivering = [
        r["name"]
        for r in config["receivers"]
        if any(key.endswith("_configs") for key in r)
    ]

    assert delivering, (
        "no receiver has any delivery method — every alert would be accepted "
        "and discarded"
    )


def test_resolved_alerts_are_sent_too():
    """An alert that never says it recovered trains you to ignore the ones
    that matter: you are left checking by hand whether it fixed itself."""
    config = _alertmanager_config()

    for receiver in config["receivers"]:
        for email in receiver.get("email_configs", []):
            assert email.get("send_resolved") is True, (
                f"receiver {receiver['name']!r} never reports recovery"
            )


def test_the_smtp_password_is_not_in_the_committed_config():
    """This file is in git. The password is a real credential to a real
    mailbox, and a repository is the one place it must never be."""
    raw = (DEPLOY / "alertmanager.yml").read_text(encoding="utf-8")

    assert "auth_password:" not in raw, (
        "the SMTP password is written into a committed file — use "
        "auth_password_file and keep the secret on the server only"
    )
    assert "auth_password_file:" in raw, "no SMTP password source configured at all"


def test_the_password_file_is_ignored_by_git():
    """The other half of the same protection: the file the config points at
    must be one git will never pick up."""
    config = _alertmanager_config()
    referenced = {
        email["auth_password_file"]
        for receiver in config["receivers"]
        for email in receiver.get("email_configs", [])
        if "auth_password_file" in email
    }
    ignored = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")

    for path in referenced:
        assert Path(path).name in ignored, (
            f"{path} holds a live SMTP password and is not in .gitignore"
        )
