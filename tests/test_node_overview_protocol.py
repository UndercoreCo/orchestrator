from dataclasses import replace

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from test_agent_gateway import pilot
from test_connection_contract import HEADERS
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.interfaces.http.specification import document


def test_node_overview_reports_protocol_from_registry_without_guessing_name(pilot):
    gateway, _, _, _ = pilot
    node = next(iter(gateway.nodes.values()))
    gateway.nodes["opaque-id"] = replace(node, id="opaque-id", protocol="trusttunnel")
    client = TestClient(create_agent_app(gateway, token="b" * 40))
    response = client.get("/internal/v1/nodes", headers=HEADERS)
    assert response.status_code == 200
    nodes = {n["id"]: n for n in response.json()["nodes"]}
    assert nodes["lab"]["protocol"] == "amneziawg"
    assert nodes["opaque-id"]["protocol"] == "trusttunnel"
    spec = document()
    Draft202012Validator(
        {**spec["components"]["schemas"]["Nodes"], "components": spec["components"]}
    ).validate(response.json())
    assert "api_key" not in response.text
    assert "api_url" not in response.text
