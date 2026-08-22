from __future__ import annotations

from fastapi.testclient import TestClient


def test_dashboard_html_has_brand_meta(hub: TestClient) -> None:
    r = hub.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert 'property="og:title"' in body
    assert "http://testserver/og.png" in body
    assert 'rel="icon"' in body
    assert "__PUBLIC_URL__" not in body


def test_pair_page_has_og_image(hub: TestClient) -> None:
    r = hub.get("/pair")
    assert r.status_code == 200
    assert "http://testserver/og.png" in r.text


def test_brand_files_public(hub: TestClient) -> None:
    cases = [
        ("/favicon.ico", "image/x-icon"),
        ("/favicon.svg", "image/svg+xml"),
        ("/apple-touch-icon.png", "image/png"),
        ("/og.png", "image/png"),
    ]
    for path, media in cases:
        r = hub.get(path)
        assert r.status_code == 200
        assert media in r.headers["content-type"]
        assert len(r.content) > 0
    og = hub.get("/og.png")
    assert len(og.content) >= 1000


def test_head_dashboard_and_og(hub: TestClient) -> None:
    assert hub.head("/").status_code == 200
    assert hub.head("/og.png").status_code == 200
