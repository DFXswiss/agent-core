from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _extract(html: str, name: str) -> str:
    start = html.index(f"    function {name}(")
    end = html.index("\n    }\n", start) + len("\n    }\n")
    return html[start:end]


def _index_html() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_core"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")


def test_request_id_final_statuses() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    function = _extract(_index_html(), "requestIdIsFinal")
    script = f"""
{function}
const cases = [
  [200, "", true],
  [201, "", true],
  [400, "bad request", true],
  [403, "forbidden", true],
  [404, "not found", true],
  [409, "sign in on the website again", true],
  [409, "request id was used for different content; reload the assignment list", true],
  [409, "request is still in progress; retry shortly", false],
  [409, "request already completed; reload the assignment list", false],
  [500, "server error", false],
  [502, "ambiguous failure without url", false],
  [502, "ambiguous failure with url", false],
  [0, "AbortError", false],
  [0, "network error", false],
];
process.stdout.write(JSON.stringify(cases.map(([status, detail, expected]) => ({{
  status,
  detail,
  expected,
  actual: requestIdIsFinal(status, detail),
}}))));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    cases = json.loads(result.stdout)
    assert all(case["actual"] == case["expected"] for case in cases), cases


def test_assignment_handlers_use_request_id_final_decision() -> None:
    submit = _index_html().split('document.addEventListener("submit"', 1)[1]
    assign_handler, reply_handler = submit.split(
        'if (form.classList.contains("reply-form"))', 1
    )

    for handler in (assign_handler, reply_handler):
        assert "requestIdIsFinal(res.status, failure.detail)" in handler
        assert 'requestIdIsFinal(res.status, "")' in handler
        assert "pendingRequestId(pendingStorage, pendingKey, payloadJson, form.dataset.requestId)" in handler
        assert "forgetPendingRequest(pendingStorage, pendingKey)" in handler
        catch = handler.split("catch (e)", 1)[1].split("finally", 1)[0]
        assert "delete form.dataset.requestId" not in catch


def test_pending_request_ids_survive_reload_and_storage_errors() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
    functions = "\n".join(
        _extract(html, name)
        for name in (
            "pendingRequestKey",
            "pendingRequestStorage",
            "pendingRequestId",
            "forgetPendingRequest",
        )
    )
    script = f"""
const crypto = require("node:crypto");
{functions}
class StubStorage {{
  constructor() {{ this.items = new Map(); }}
  getItem(key) {{ return this.items.has(key) ? this.items.get(key) : null; }}
  setItem(key, value) {{ this.items.set(key, value); }}
  removeItem(key) {{ this.items.delete(key); }}
}}
class ThrowingStorage {{
  getItem() {{ throw new Error("unavailable"); }}
  setItem() {{ throw new Error("unavailable"); }}
  removeItem() {{ throw new Error("unavailable"); }}
}}
const storage = new StubStorage();
const formKey = JSON.stringify(["assign", "helperbot"]);
const key = pendingRequestKey("alice", formKey);
const payload = JSON.stringify({{agent: "helperbot", repo: "Acme/Widget", title: "T", body: "B"}});
const changedPayload = JSON.stringify({{agent: "helperbot", repo: "Acme/Widget", title: "Changed", body: "B"}});
const first = pendingRequestId(storage, key, payload);
const stored = JSON.parse(storage.getItem(key));
const reused = pendingRequestId(storage, key, payload);
const replaced = pendingRequestId(storage, key, changedPayload);
const replacement = JSON.parse(storage.getItem(key));
forgetPendingRequest(storage, key);
const forgotten = storage.getItem(key);
const throwing = new ThrowingStorage();
const fallback = pendingRequestId(throwing, "key", payload);
const keptFormId = pendingRequestId(throwing, "key", payload, "kept-form-id");
forgetPendingRequest(throwing, "key");
Object.defineProperty(globalThis, "sessionStorage", {{
  configurable: true,
  get() {{ throw new Error("blocked"); }},
}});
const unavailable = pendingRequestStorage();
process.stdout.write(JSON.stringify({{
  key,
  first,
  stored,
  reused,
  replaced,
  replacement,
  forgotten,
  fallback,
  keptFormId,
  unavailable,
}}));
"""
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True, timeout=30
    )
    out = json.loads(result.stdout)
    assert out["keptFormId"] == "kept-form-id"
    expected_payload = json.dumps(
        {
            "agent": "helperbot",
            "repo": "Acme/Widget",
            "title": "T",
            "body": "B",
        },
        separators=(",", ":"),
    )
    assert out["key"] == 'agent-core:pending:alice:["assign","helperbot"]'
    assert out["stored"] == {
        "request_id": out["first"],
        "payload": expected_payload,
    }
    assert out["reused"] == out["first"]
    assert out["replaced"] != out["first"]
    assert out["replacement"]["request_id"] == out["replaced"]
    assert out["replacement"]["payload"] != out["stored"]["payload"]
    assert out["forgotten"] is None
    assert isinstance(out["fallback"], str) and len(out["fallback"]) == 36
    assert out["unavailable"] is None


def test_dashboard_uses_only_query_session_api_routes() -> None:
    html = _index_html()
    assert "/api/sessions/" not in html
    assert 'fetch("/api/session?id=" + encodeURIComponent(id))' in html
    assert 'new EventSource("/api/session/terminal?id=" + encodeURIComponent(id))' in html
    assert 'fetch("/api/session/control?id=" + encodeURIComponent(openSessionId)' in html


def test_clear_hub_tables_does_not_forget_pending_requests() -> None:
    assert "sessionStorage" not in _extract(_index_html(), "clearHubTables")


def test_pair_selector_lists_login_then_agents_keeps_selection_and_builds_payload() -> None:
    node = shutil.which("node")
    # Node is a test requirement (README); a missing node must fail, not skip.
    assert node is not None, "node is required to run the dashboard page test"
    html = _index_html()
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
