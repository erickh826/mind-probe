"""The image installs requirements.txt. It must list every runtime dependency."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]


def _dist_name(requirement: str) -> str:
    name = requirement.split(";", 1)[0].strip()
    return re.split(r"[\s\[<>=!~]", name, maxsplit=1)[0].lower()


def _requirement_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(_dist_name(line))
    return names


def test_requirements_txt_includes_every_runtime_dependency():
    project = tomllib.loads((SERVER / "pyproject.toml").read_text())
    runtime = {_dist_name(dep) for dep in project["project"]["dependencies"]}
    installed = _requirement_names((SERVER / "requirements.txt").read_text())
    assert runtime <= installed
    assert "boto3" in installed

    dockerfile = (SERVER / "Dockerfile").read_text()
    assert "requirements.txt" in dockerfile
    assert "import boto3" in dockerfile
