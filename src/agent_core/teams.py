"""Load the hardcoded team roster. Fail closed on a broken file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class TeamsError(ValueError):
    pass


def load_teams(path: Path) -> dict[str, list[str]]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TeamsError(f"teams file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict) or "teams" not in data:
        raise TeamsError(f"teams file must have a top-level 'teams' map: {path}")
    teams_raw = data["teams"]
    if not isinstance(teams_raw, dict):
        raise TeamsError(f"'teams' must be a map: {path}")
    out: dict[str, list[str]] = {}
    for name, spec in teams_raw.items():
        if not isinstance(name, str) or name == "":
            raise TeamsError("team name must be a non-empty string")
        if not isinstance(spec, dict) or "members" not in spec:
            raise TeamsError(f"team {name!r} must have a 'members' list")
        members = spec["members"]
        if not isinstance(members, list):
            raise TeamsError(f"team {name!r} members must be a list")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in members:
            if not isinstance(item, str) or item.strip() == "":
                raise TeamsError(f"team {name!r} has a non-string or empty member")
            login = item.strip().lower()
            if login in seen:
                raise TeamsError(f"team {name!r} lists {login} twice")
            seen.add(login)
            cleaned.append(login)
        out[name] = cleaned
    return out


def visible_logins(login: str, teams: dict[str, list[str]]) -> set[str]:
    self = login.strip().lower()
    if self == "":
        raise TeamsError("login must be a non-empty string")
    visible = {self}
    for members in teams.values():
        if self in members:
            visible.update(members)
    return visible


def dump_shape(data: Any) -> str:
    return type(data).__name__
