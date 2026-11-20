"""The REST rows owned by WP1 (ARCHITECTURE.md section 6), on a temporary copy of the agents."""
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.definition import DefinitionStore
from agentkit.definition.api import router

REAL_AGENTS = Path(__file__).resolve().parent.parent / "agents"
SHOP = "shop-support"


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    shutil.copytree(REAL_AGENTS, root)
    monkeypatch.setenv("AGENTKIT_AGENTS_DIR", str(root))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        c.store = DefinitionStore(root)
        yield c


def test_list_agents(client):
    body = client.get("/api/agents").json()
    assert [a["id"] for a in body] == client.store.list_agents() and len(body) == 7
    shop = next(a for a in body if a["id"] == SHOP)
    assert shop["name"] == "쇼핑몰 주문 상담" and shop["stable"] == 1
    assert shop["deployment"]["stable"] == 1 and shop["versions"][0]["state"] == "deployed"
    clinic = next(a for a in body if a["id"] == "clinic-intake")
    assert clinic["stable"] is None and clinic["versions"][0]["state"] in ("draft", "tested") and clinic["name"]   # not deployed
    anchor = next(a for a in body if a["id"] == "tech-trend-anchor")
    assert anchor["avatar"] is True and len(anchor["tools"]) == 7


def test_broken_agent_does_not_break_the_list(client):
    (client.store.root / SHOP / "versions/1/flow.yaml").write_text("flow: {", encoding="utf-8")
    body = client.get("/api/agents").json()
    assert len(body) == 7
    assert "flow.yaml" in next(a for a in body if a["id"] == SHOP)["error"]
    response = client.get(f"/api/agents/{SHOP}/versions/1")
    assert response.status_code == 422
    assert response.json()["problems"][0]["file"] == "flow.yaml"


def test_agent_detail(client):
    body = client.get(f"/api/agents/{SHOP}").json()
    assert body["id"] == SHOP and body["deployment"]["agent_id"] == SHOP
    assert body["versions"][0]["version"] == 1 and "safety_change" in body["versions"][0]
    assert client.get("/api/agents/nope").status_code == 404


def test_version_is_the_whole_definition(client):
    body = client.get(f"/api/agents/{SHOP}/versions/1").json()
    assert body["id"] == SHOP and body["version"] == 1
    assert body["tools"][0] == "verify_customer" and body["evaluation"]["success"] and body["tests"]["personas"]
    assert client.get(f"/api/agents/{SHOP}/versions/9").status_code == 404
    assert client.get(f"/api/agents/{SHOP}/versions/abc").status_code == 422


def test_diff(client):
    n = client.store.create_draft(SHOP, 1, "builder")
    d = client.store.load(SHOP, n)
    d.handoff[0].when = "환불 금액이 50000원을 넘는다"
    d.reply_length = "normal"
    client.store.save_draft(d)
    body = client.get(f"/api/agents/{SHOP}/diff", params={"a": 1, "b": n}).json()
    assert set(body["changes"]) == {"handoff", "reply_length"} == set(body["changed_fields"])
    assert body["changes"]["reply_length"] == {"before": "short", "after": "normal"}
    assert body["safety_change"] is True and body["safety_fields"] == ["handoff"]
    assert client.get(f"/api/agents/{SHOP}/diff", params={"a": 1, "b": 9}).status_code == 404
    assert client.get(f"/api/agents/{SHOP}/diff", params={"a": 1}).status_code == 422


def test_release_flow_over_rest(client):
    n = client.store.create_draft(SHOP, 1, "builder", "시험")
    base = f"/api/releases/{SHOP}/{n}"

    refused = client.post(f"{base}/submit")                     # no test run
    assert refused.status_code == 409 and refused.json()["detail"]
    refused = client.post(f"{base}/deploy", json={"percent": 100})   # not approved
    assert refused.status_code == 409 and "승인" in refused.json()["detail"]

    client.store.attach_test_run(SHOP, n, "run-1")
    done = client.post(f"{base}/submit")
    assert done.status_code == 200 and done.json()["version"]["state"] == "pending_approval"

    assert client.post(f"{base}/approve").status_code == 409                        # no approver
    assert client.post(f"{base}/approve", json={}).status_code == 409
    assert client.post(f"{base}/approve", json={"approver": "builder"}).status_code == 409
    assert client.post(f"{base}/deploy", json={}).status_code == 409               # still not approved
    done = client.post(f"{base}/approve", json={"approver": "김담당"})
    assert done.status_code == 200
    assert done.json()["version"]["state"] == "approved" and done.json()["version"]["approved_by"] == "김담당"

    assert client.post(f"{base}/deploy", json={"percent": 0}).status_code == 409
    canary = client.post(f"{base}/deploy", json={"percent": 20}).json()
    assert canary["ok"] and canary["deployment"]["canary"] == n and canary["deployment"]["canary_percent"] == 20
    assert canary["deployment"]["stable"] == 1 and canary["version"]["state"] == "deployed"

    full = client.post(f"{base}/deploy").json()                 # no body: 100
    assert full["deployment"]["stable"] == n and full["deployment"]["previous_stable"] == 1
    assert [v["state"] for v in full["versions"]] == ["retired", "deployed"]

    back = client.post(f"/api/releases/{SHOP}/rollback")
    assert back.status_code == 200
    assert back.json()["deployment"]["stable"] == 1 and back.json()["version"]["version"] == 1
    assert [v["state"] for v in back.json()["versions"]] == ["deployed", "retired"]

    again = client.post(f"/api/releases/{SHOP}/rollback")
    assert again.status_code == 409 and again.json()["detail"]


def test_release_on_unknown_agent_or_version(client):
    assert client.post("/api/releases/nope/1/submit").status_code == 404
    assert client.post(f"/api/releases/{SHOP}/9/deploy", json={"percent": 100}).status_code == 404
    assert client.post("/api/releases/nope/rollback").status_code == 404
