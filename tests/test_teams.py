from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_core.teams import TeamsError, load_roster, load_teams, visible_logins


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("teams: [", "not valid YAML"),
        ([], "top-level 'teams' map"),
        ({"other": 1}, "top-level 'teams' map"),
        ({"teams": []}, "'teams' must be a map"),
        ({"teams": {"": {"members": []}}}, "team name must be a non-empty string"),
        ({"teams": {"dfx": []}}, "must have a 'members' list"),
        ({"teams": {"dfx": {}}}, "must have a 'members' list"),
        ({"teams": {"dfx": {"members": "alice"}}}, "members must be a list"),
        ({"teams": {"dfx": {"members": [None]}}}, "non-string or empty member"),
        ({"teams": {"dfx": {"members": ["alice", "ALICE"]}}}, "lists alice twice"),
        ({"teams": {}, "agents": []}, "'agents' must be a map"),
        (
            {"teams": {"dfx": {"members": ["owner"]}}, "agents": {"": {"owners": []}}},
            "agent login must be a non-empty string",
        ),
        (
            {
                "teams": {"dfx": {"members": ["bot"]}},
                "agents": {"Bot": {"owners": []}, "bot": {"owners": []}},
            },
            "agents map lists bot twice",
        ),
        (
            {"teams": {"dfx": {"members": ["owner"]}}, "agents": {"bot": {"owners": ["owner"]}}},
            "must be a member",
        ),
        (
            {"teams": {"dfx": {"members": ["bot"]}}, "agents": {"bot": []}},
            "must have an 'owners' list",
        ),
        (
            {"teams": {"dfx": {"members": ["bot"]}}, "agents": {"bot": {}}},
            "must have an 'owners' list",
        ),
        (
            {"teams": {"dfx": {"members": ["bot"]}}, "agents": {"bot": {"owners": "owner"}}},
            "owners must be a list",
        ),
        (
            {"teams": {"dfx": {"members": ["bot"]}}, "agents": {"bot": {"owners": [None]}}},
            "non-string or empty owner",
        ),
        (
            {
                "teams": {"dfx": {"members": ["bot", "owner"]}},
                "agents": {"bot": {"owners": ["owner", "OWNER"]}},
            },
            "lists owner owner twice",
        ),
        (
            {"teams": {"dfx": {"members": ["bot"]}}, "agents": {"bot": {"owners": ["bot"]}}},
            "cannot own itself",
        ),
        (
            {
                "teams": {"one": {"members": ["bot"]}, "two": {"members": ["owner"]}},
                "agents": {"bot": {"owners": ["owner"]}},
            },
            "must share a team",
        ),
    ],
)
def test_roster_error_branches_are_fail_closed(
    tmp_path: Path, document: object, message: str
) -> None:
    path = tmp_path / "invalid-teams.yaml"
    raw = document if isinstance(document, str) else yaml.safe_dump(document)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(TeamsError, match=message):
        load_roster(path)


def test_visible_logins_rejects_an_empty_login() -> None:
    with pytest.raises(TeamsError, match="login must be a non-empty string"):
        visible_logins(" ", {})


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
        "joshuakrueger-dfx",
        "jonnyluca",
        "theo-vane",
    ]
    roster = load_roster(path)
    assert roster.agents == {"jonnyluca": frozenset({"taprootfreak", "taprootfreakai"})}


def test_missing_teams_key(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text("other: 1\n", encoding="utf-8")
    with pytest.raises(TeamsError, match="top-level"):
        load_teams(path)


def test_valid_agents_block(tmp_path: Path) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "teams": {"dfx": {"members": ["Owner", "AgentBot"]}},
                "agents": {"AgentBot": {"owners": ["Owner"]}},
            }
        ),
        encoding="utf-8",
    )
    roster = load_roster(path)
    assert roster.agents == {"agentbot": frozenset({"owner"})}
    assert roster.is_agent("AgentBot")
    assert roster.agent_owners("agentbot") == {"owner"}
    assert roster.owns_agent("OWNER", "AGENTBOT")


@pytest.mark.parametrize(
    ("teams", "agents", "message"),
    [
        ({"dfx": {"members": ["owner"]}}, {"bot": {"owners": ["owner"]}}, "member"),
        (
            {"one": {"members": ["bot"]}, "two": {"members": ["owner"]}},
            {"bot": {"owners": ["owner"]}},
            "share a team",
        ),
        ({"dfx": {"members": ["bot"]}}, {"bot": {"owners": ["bot"]}}, "own itself"),
        (
            {"dfx": {"members": ["bot", "owner"]}},
            {"bot": {"owners": ["owner", "OWNER"]}},
            "twice",
        ),
    ],
)
def test_invalid_agents_fail_closed(tmp_path: Path, teams: dict, agents: dict, message: str) -> None:
    path = tmp_path / "teams.yaml"
    path.write_text(yaml.safe_dump({"teams": teams, "agents": agents}), encoding="utf-8")
    with pytest.raises(TeamsError, match=message):
        load_roster(path)
