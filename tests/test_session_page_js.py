from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("pathname", "search", "session_id", "timeline_url"),
    [
        (
            "/session",
            "?id=a%2F..%2Fb",
            "a/../b",
            "/api/session-timeline?id=a%2F..%2Fb",
        ),
        (
            "/sessions/abc%20d",
            "",
            "abc d",
            "/api/session-timeline?id=abc%20d",
        ),
    ],
)
def test_session_page_shows_signed_out_notice_and_decodes_session_id(
    pathname: str, search: str, session_id: str, timeline_url: str
) -> None:
    node = shutil.which("node")
    # Node is a test requirement (README); a missing node must fail, not skip.
    assert node is not None, "node is required to run the session page test"

    html = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_core"
        / "static"
        / "session.html"
    ).read_text(encoding="utf-8")
    start = html.index("    function showSignedOut()")
    end = html.index("    load();", start)
    functions = html[start:end]
    script = f"""
class StubElement {{
  constructor(id) {{
    this.id = id;
    this.textContent = "";
    this.href = "";
    this.children = [];
    this.classes = new Set();
    this.classList = {{
      add: value => this.classes.add(value),
      remove: value => this.classes.delete(value),
    }};
  }}
  replaceChildren(...children) {{
    this.children = children;
    this.textContent = children.map(child => typeof child === "string" ? child : child.textContent).join("");
  }}
}}

const elements = new Map([
  ["notice", new StubElement("notice")],
  ["session-id", new StubElement("session-id")],
]);
global.document = {{
  getElementById: id => elements.get(id),
  createElement: tag => new StubElement(tag),
}};
let fetchedUrl = "";
global.fetch = async url => {{
  fetchedUrl = url;
  return {{status: 401, ok: false}};
}};
global.location = {{pathname: {json.dumps(pathname)}, search: {json.dumps(search)}}};

{functions}

load().then(() => {{
  const notice = elements.get("notice");
  const link = notice.children[1];
  process.stdout.write(JSON.stringify({{
    notice: notice.textContent,
    href: link.href,
    sessionId: elements.get("session-id").textContent,
    fetchedUrl,
  }}));
}}).catch(error => {{
  process.stderr.write(String(error));
  process.exitCode = 1;
}});
"""

    completed = subprocess.run(
        [node],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "notice": "Signed out — sign in on the dashboard",
        "href": "/",
        "sessionId": session_id,
        "fetchedUrl": timeline_url,
    }
