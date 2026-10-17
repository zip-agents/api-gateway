"""Preparing OpenAPI documents for the gateway, curl samples, and the comparison."""

import copy
from typing import Any

import pytest

from gateway.docs import DocsSite
from gateway.openapi_view import Difference, compare, curl_sample, for_gateway, resolve


@pytest.fixture
def contract() -> dict[str, Any]:
    return DocsSite().contract_spec


def sample(document: dict[str, Any], path: str, method: str) -> str:
    source: str = document["paths"][path][method]["x-codeSamples"][0]["source"]
    return source


def test_for_gateway_adds_server_auth_and_gateway_errors(contract: dict[str, Any]) -> None:
    original = copy.deepcopy(contract)
    document = for_gateway(contract, [("https://gw.example/claude", "claude")], "Note.")
    assert contract == original  # the cached source document is never modified
    assert document["servers"] == [{"url": "https://gw.example/claude", "description": "claude"}]
    assert document["security"] == [{"gatewayKey": []}]
    assert set(document["components"]["securitySchemes"]) == {"gatewayKey"}
    assert document["info"]["description"].startswith("Note.")
    for item in document["paths"].values():
        for operation in item.values():
            assert {"401", "429", "502", "504"} <= set(operation["responses"])
            assert operation["x-codeSamples"][0]["lang"] == "Shell"
    for ref in ("#/components/schemas/GatewayProblem",):
        assert resolve(document, {"$ref": ref}) != {"$ref": ref}


def test_zip_sample_sends_fields_first_and_one_part_per_file(contract: dict[str, Any]) -> None:
    document = for_gateway(contract, [("https://gw.example/claude", "claude")], "")
    curl = sample(document, "/api/v1/zip", "post")
    assert '"https://gw.example/claude/api/v1/zip"' in curl
    assert "Authorization: Bearer $API_KEY" in curl
    assert curl.index("-F 'filename=quarterly report'") < curl.index("-F 'files=@a.txt'")
    assert curl.count("-F 'files=@") == 2  # repeated parts, never a JSON array
    assert "Content-Type" not in curl  # curl sets it, boundary included
    assert "-X POST" not in curl  # implied by -F
    assert "-o archive.zip" in curl


def test_samples_fill_path_parameters_and_skip_needless_flags(contract: dict[str, Any]) -> None:
    document = for_gateway(contract, [("http://gw/claude", "claude")], "")
    assert '"http://gw/claude/docs/README.md"' in sample(document, "/docs/{name}", "get")
    health = sample(document, "/health/live", "get")
    assert "-X" not in health
    assert "-o" not in health


def test_several_servers_leave_the_base_url_to_the_reader(contract: dict[str, Any]) -> None:
    document = for_gateway(contract, [("http://gw/claude", "a"), ("http://gw/grok", "b")], "")
    curl = sample(document, "/api/v1/zip", "post")
    assert '"$BASE_URL/api/v1/zip"' in curl
    assert "BASE_URL: one of the servers above" in curl


@pytest.mark.parametrize(
    ("method", "body", "expected"),
    [
        ("delete", None, "-X DELETE"),
        ("post", None, "-X POST"),
        ("put", {"application/json": {"schema": {"example": {"a": 1}}}}, "-d '{\"a\": 1}'"),
    ],
)
def test_samples_for_other_methods(method: str, body: Any, expected: str) -> None:
    operation: dict[str, Any] = {"responses": {"204": {"description": "ok"}}}
    if body is not None:
        operation["requestBody"] = {"content": body}
    assert expected in curl_sample({}, "http://gw/x", method, "/things", operation)


def test_shell_quoting() -> None:
    operation = {
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"properties": {"name": {"type": "string", "example": "it's"}}}
                }
            }
        }
    }
    assert "-F 'name=it'\\''s'" in curl_sample({}, "http://gw/x", "post", "/p", operation)


def test_resolve_follows_local_refs_and_leaves_others() -> None:
    spec = {"components": {"schemas": {"A": {"$ref": "#/components/schemas/B"}, "B": {"x": 1}}}}
    assert resolve(spec, {"$ref": "#/components/schemas/A"}) == {"x": 1}
    assert resolve(spec, {"$ref": "#/components/schemas/missing"}) == {
        "$ref": "#/components/schemas/missing"
    }
    assert resolve(spec, {"$ref": "other.yaml#/A"}) == {"$ref": "other.yaml#/A"}


def test_identical_documents_do_not_differ(contract: dict[str, Any]) -> None:
    assert compare(contract, copy.deepcopy(contract)) == []


def test_compare_reports_each_kind_of_difference(contract: dict[str, Any]) -> None:
    service = copy.deepcopy(contract)
    zip_post = service["paths"]["/api/v1/zip"]["post"]
    del zip_post["responses"]["413"]  # e.g. a service that never sends it
    zip_post["responses"]["429"] = {"description": "slow down"}
    schema = zip_post["requestBody"]["content"]["multipart/form-data"]["schema"]
    schema["properties"]["comment"] = {"type": "string"}
    schema["required"] = ["files", "comment"]
    service["paths"]["/health/live"]["get"]["responses"]["200"]["content"] = {
        "text/plain": {"schema": {"type": "string"}}
    }
    del service["paths"]["/health/ready"]
    service["paths"]["/metrics"] = {"get": {"responses": {"200": {"description": "m"}}}}
    # A different path parameter name is the same route.
    service["paths"]["/docs/{doc}"] = service["paths"].pop("/docs/{name}")

    found = set(compare(contract, service))
    assert found == {
        Difference("POST /api/v1/zip", "response status", "413", "not documented"),
        Difference("POST /api/v1/zip", "response status", "", "429"),
        Difference(
            "POST /api/v1/zip",
            "request fields (multipart/form-data)",
            "filename, files",
            "comment, filename, files",
        ),
        Difference(
            "POST /api/v1/zip", "required fields (multipart/form-data)", "files", "comment, files"
        ),
        Difference("GET /health/live", "success media type", "application/json", "text/plain"),
        Difference("GET /health/ready", "operation", "documented", "missing"),
        Difference("GET /metrics", "operation", "", "documented (not in the contract)"),
    }
