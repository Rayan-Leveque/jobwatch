"""Tests des chemins d'instances isolées."""

from pathlib import Path

import pytest

from jobwatch.paths import instance_paths, validate_instance_name


@pytest.mark.parametrize("name", ["rayan", "alice-2", "ami_test", "7"])
def test_validate_instance_name_accepts_safe_slugs(name: str) -> None:
    assert validate_instance_name(name) == name


@pytest.mark.parametrize("name", ["", "Alice", "../alice", "ami test", "éloïse", "a" * 65])
def test_validate_instance_name_rejects_unsafe_slugs(name: str) -> None:
    with pytest.raises(ValueError):
        validate_instance_name(name)


def test_instance_paths_use_xdg_homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    paths = instance_paths("alice")
    assert paths.config == config_home / "jobwatch/instances/alice/config.yaml"
    assert paths.data_dir == data_home / "jobwatch/instances/alice"
    assert paths.db == data_home / "jobwatch/instances/alice/jobwatch.db"
