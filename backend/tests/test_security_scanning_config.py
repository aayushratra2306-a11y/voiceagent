"""Task 6.7 — the scanning configuration itself, not the scan results.

.github/dependabot.yml and .github/workflows/ci.yml are YAML that nothing
imports, so a typo in either is invisible until the exact moment it
matters: the workflow file fails to parse on GitHub, or dependabot.yml
silently stops covering an ecosystem this project actually depends on.
Mirrors the same reasoning test_monitoring_config.py already applies to
the Prometheus/Grafana config from task 4.9.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dependabot_config_parses():
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    assert config["updates"], "dependabot.yml declares no update targets at all"


@pytest.mark.parametrize("ecosystem", ["pip", "npm", "docker", "github-actions"])
def test_every_real_dependency_manifest_this_project_has_is_covered(ecosystem):
    """The project has a Python backend, a Node frontend, a Dockerfile, and
    GitHub Actions workflows — each is a real place a vulnerable pin can
    sit, and each needs its own entry or Dependabot simply never looks
    there."""
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = [u["package-ecosystem"] for u in config["updates"]]
    assert ecosystem in ecosystems, f"{ecosystem!r} has no dependabot.yml entry"


def test_every_update_target_actually_exists_on_disk():
    """A directory that does not exist is a config entry that will never
    find anything to update — silently, since dependabot.yml is not
    something a normal test run touches at all."""
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    for update in config["updates"]:
        directory = update["directory"].lstrip("/")
        target = REPO_ROOT / directory if directory else REPO_ROOT
        assert target.is_dir(), f"{update['package-ecosystem']} points at a directory that doesn't exist: {target}"


def test_the_pip_target_actually_points_at_requirements_txt():
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    pip_entry = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
    directory = REPO_ROOT / pip_entry["directory"].lstrip("/")
    assert (directory / "requirements.txt").is_file()


def test_every_target_has_an_update_schedule():
    """An entry with no schedule is invalid config that dependabot.yml's
    own schema would reject outright — checked here so that failure shows
    up in this test suite instead of only on GitHub after a push."""
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    for update in config["updates"]:
        assert "schedule" in update and update["schedule"].get("interval"), (
            f"{update['package-ecosystem']} has no update schedule"
        )


def test_ci_still_runs_a_secret_scan_and_a_dependency_scan():
    """The two scanning JOBS this task actually asked for, confirmed to
    still exist by name — a refactor of ci.yml that quietly dropped one
    would otherwise only be noticed the next time a real secret or a real
    vulnerable pin slipped through."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job_names = set(workflow["jobs"])
    assert "secret-scan" in job_names
    assert "dependency-scan" in job_names
