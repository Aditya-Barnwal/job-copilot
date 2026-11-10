"""Config loader: defaults, deep merge, legacy companies path."""
from __future__ import annotations

import pytest

from jobcopilot import config


def test_defaults_when_files_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(tmp_path))
    f = config.filters()
    assert f["salary_floors"]["INR"] == 1_500_000
    assert f["yoe"]["max_required"] == 2
    assert config.schedule()["daily_run"] == "19:00"
    assert "job-copilot" in config.sources()["http"]["user_agent"]


def test_file_overrides_are_deep_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "filters.yaml").write_text("salary_floors:\n  USD: 50000\n", encoding="utf-8")
    f = config.filters()
    assert f["salary_floors"]["USD"] == 50000
    assert f["salary_floors"]["INR"] == 1_500_000  # untouched sibling keeps default
    assert config.DEFAULTS["filters"]["salary_floors"]["USD"] == 30_000  # defaults not mutated


def test_non_mapping_file_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "schedule.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.schedule()


def test_repo_config_files_parse():
    # The committed config/*.yaml must always load cleanly.
    for name in ("filters", "sources", "schedule"):
        assert isinstance(config.load(name), dict)
    assert config.companies_path().name == "companies.yaml"


def test_companies_path_prefers_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBCOPILOT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "companies.yaml").write_text("companies: []\n", encoding="utf-8")
    assert config.companies_path() == tmp_path / "companies.yaml"
