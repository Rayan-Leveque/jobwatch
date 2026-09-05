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
