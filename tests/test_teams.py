from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_core.teams import TeamsError, load_teams, visible_logins


def test_load_and_visibility(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(
        yaml.safe_dump({"teams": {"dfx": {"members": ["Alice", "bob"]}, "ops": {"members": ["bob", "cara"]}}}),
        encoding="utf-8",
    )
    teams = load_teams(path)
    assert teams["dfx"] == ["alice", "bob"]
    assert visible_logins("alice", teams) == {"alice", "bob"}
    assert visible_logins("bob", teams) == {"alice", "bob", "cara"}
    assert visible_logins("dave", teams) == {"dave"}


def test_missing_members_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(yaml.safe_dump({"teams": {"dfx": {}}}), encoding="utf-8")
    with pytest.raises(TeamsError, match="members"):
        load_teams(path)


def test_duplicate_member_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(yaml.safe_dump({"teams": {"dfx": {"members": ["a", "A"]}}}), encoding="utf-8")
    with pytest.raises(TeamsError, match="twice"):
        load_teams(path)


def test_checked_in_teams_yaml_loads() -> None:
    path = Path(__file__).resolve().parents[1] / "teams.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    listed = raw["teams"]["dfx"]["members"]
    assert listed == [m.lower() for m in listed]
    assert listed == list(dict.fromkeys(listed))
    assert "taprootfreak" in listed
    assert "davidleomay" in listed
    teams = load_teams(path)
    assert teams["dfx"] == [
        "taprootfreak",
        "davidleomay",
        "taprootfreakai",
        "marassteiner",
        "danswar",
    ]


def test_missing_teams_key(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text("other: 1\n", encoding="utf-8")
    with pytest.raises(TeamsError, match="top-level"):
        load_teams(path)
