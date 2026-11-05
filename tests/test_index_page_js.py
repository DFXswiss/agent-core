from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _extract(html: str, name: str) -> str:
    start = html.index(f"    function {name}(")
    end = html.index("\n    }\n", start) + len("\n    }\n")
    return html[start:end]


def test_pair_selector_lists_login_then_agents_keeps_selection_and_builds_payload() -> None:
    node = shutil.which("node")
    # Node is a test requirement (README); a missing node must fail, not skip.
    assert node is not None, "node is required to run the dashboard page test"
    html = (
        Path(__file__).resolve().parents[1] / "src" / "agent_core" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    functions = "\n".join(
        _extract(html, name)
        for name in ("esc", "pairChoices", "fillPairSelect", "pairConfirmPayload")
    )
    script = f"""
class StubSelect {{
  constructor() {{ this.options = []; this._value = ""; }}
  set innerHTML(markup) {{
    this.markup = markup;
    this.options = [...markup.matchAll(/<option value="([^"]*)">/g)].map(match => match[1]);
    this._value = this.options[0] || "";
  }}
  get value() {{ return this._value; }}
  set value(next) {{ if (this.options.includes(next)) this._value = next; }}
}}
{functions}
const user = {{login: "alice", owned_agents: ["alphabot", "zetabot"]}};
const select = new StubSelect();
fillPairSelect(select, user);
const first = {{options: select.options, value: select.value}};
select.value = "zetabot";
fillPairSelect(select, user);
const kept = select.value;
fillPairSelect(select, {{login: "alice", owned_agents: ["alphabot"]}});
const dropped = select.value;
fillPairSelect(select, {{login: "bob"}});
const noAgents = select.options;
process.stdout.write(JSON.stringify({{
  first,
  kept,
  dropped,
  noAgents,
  self: pairConfirmPayload("c1", "alice", "alice"),
  agent: pairConfirmPayload("c2", "zetabot", "alice"),
  empty: pairConfirmPayload("c3", "", "alice"),
}}));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["first"] == {"options": ["alice", "alphabot", "zetabot"], "value": "alice"}
    assert out["kept"] == "zetabot"
    assert out["dropped"] == "alice"
    assert out["noAgents"] == ["bob"]
    assert out["self"] == {"challenge": "c1"}
    assert out["agent"] == {"challenge": "c2", "as_login": "zetabot"}
    assert out["empty"] == {"challenge": "c3"}
