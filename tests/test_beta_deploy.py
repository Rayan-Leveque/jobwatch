"""Exécute le script de déploiement avec services simulés et vrais liens de version."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("healthy", [True, False])
def test_deploy_switches_release_or_rolls_back(tmp_path, healthy):
    root = tmp_path / "jobwatch"
    old = root / "releases" / ("a" * 40)
    new = root / "releases" / ("b" * 40)
    for release in (old, new):
        (release / ".venv/bin").mkdir(parents=True)
        binary = release / ".venv/bin/jw"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    (root / "current").symlink_to(old)
    config = tmp_path / "etc/instances/alice"
    config.mkdir(parents=True)
    (config / "service.env").write_text("PORT=8801\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls"
    for command, content in {
        "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_LOG"\n',
        "curl": f"#!/bin/sh\nexit {0 if healthy else 1}\n",
    }.items():
        path = commands / command
        path.write_text(content)
        path.chmod(0o755)
    script = tmp_path / "deploy.sh"
    original = Path("ops/deploy.sh").read_text()
    script.write_text(original.replace("/opt/jobwatch", str(root))
                     .replace("/run/lock", str(tmp_path))
                     .replace("/etc/jobwatch", str(tmp_path / "etc")))
    result = subprocess.run(["bash", str(script), str(new), "alice"],
                            env={**os.environ, "PATH": f"{commands}:/usr/bin:/bin", "TEST_LOG": str(log)},
                            capture_output=True, text=True, timeout=10, check=False)
    assert (result.returncode == 0) is healthy, result.stderr
    assert (root / "current").resolve() == (new if healthy else old)
    assert "start jobwatch-collect@alice.timer" in log.read_text()
    if not healthy:
        assert "restart jobwatch@alice.service" in log.read_text()


def test_provision_creates_traversable_parents_and_private_instances(tmp_path):
    root = tmp_path / "opt/jobwatch"
    binary = root / "current/.venv/bin/jw"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/bash\nset -eu\n'
        'if [[ "$3" == init ]]; then\n'
        '  config="$XDG_CONFIG_HOME/jobwatch/instances/$2"\n'
        '  data="$XDG_DATA_HOME/jobwatch/instances/$2"\n'
        '  mkdir -p "$config" "$data"\n'
        '  printf "sources: {}\\n" > "$config/config.yaml"\n'
        '  touch "$data/jobwatch.db"\n'
        'fi\n'
    )
    binary.chmod(0o755)
    (root / "current/ops").mkdir()
    (root / "current/ops/nginx.conf").write_text(
        "server_name alice.jobs.example; proxy_pass http://127.0.0.1:8801;\n"
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("id", "useradd", "chown", "systemctl"):
        path = commands / command
        path.write_text(f"#!/bin/sh\nexit {1 if command == 'id' else 0}\n")
        path.chmod(0o755)
    config_root = tmp_path / "etc"
    data_root = tmp_path / "var/lib"
    config_root.mkdir(mode=0o755)
    data_root.mkdir(parents=True, mode=0o755)
    script = tmp_path / "provision.sh"
    script.write_text(
        Path("ops/provision.sh").read_text()
        .replace("/opt/jobwatch", str(root))
        .replace("/etc", str(config_root))
        .replace("/var/lib", str(data_root))
        .replace("EUID == 0", "1 == 1")
    )
    for slug in ("alice", "bob"):
        result = subprocess.run(
            ["bash", str(script), slug, f"{slug}@example.com", f"{slug}.jobs.example", "8801"],
            env={**os.environ, "PATH": f"{commands}:/usr/bin:/bin"},
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        for shared_root in (config_root, data_root):
            for parent in (shared_root / "jobwatch", shared_root / "jobwatch/instances"):
                assert parent.stat().st_mode & 0o777 == 0o755
        config = config_root / "jobwatch/instances" / slug
        data = data_root / "jobwatch/instances" / slug
        assert config.stat().st_mode & 0o777 == 0o750
        assert data.stat().st_mode & 0o777 == 0o700
        for filename in ("config.yaml", "service.env"):
            assert (config / filename).stat().st_mode & 0o777 == 0o640
        assert (data / "jobwatch.db").stat().st_mode & 0o777 == 0o600
