"""Chemins d'une instance jobwatch isolée."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

INSTANCE_ENV = "JOBWATCH_INSTANCE"
_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class InstancePaths:
    """Configuration et données propres à une personne."""

    name: str
    config: Path
    data_dir: Path
    db: Path


def validate_instance_name(name: str) -> str:
    """Valide un identifiant sûr pour un composant de chemin."""
    if not _INSTANCE_RE.fullmatch(name):
        raise ValueError(
            "doit commencer par une lettre ou un chiffre minuscule et ne contenir "
            "que a-z, 0-9, _ ou - (64 caractères maximum)"
        )
    return name


def instance_paths(name: str) -> InstancePaths:
    """Calcule les chemins XDG de l'instance nommée."""
    valid_name = validate_instance_name(name)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    config = config_home / "jobwatch" / "instances" / valid_name / "config.yaml"
    data_dir = data_home / "jobwatch" / "instances" / valid_name
    return InstancePaths(
        name=valid_name,
        config=config,
        data_dir=data_dir,
        db=data_dir / "jobwatch.db",
    )
