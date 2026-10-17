"""OpenAPI documents as the portal shows them, and how they differ from the contract.

There are two kinds of document: the contract's (docs/openapi.yaml, written by hand) and
each service's own (fetched from the service, generated from its code). Both describe a
service as it is reached directly. `for_gateway` rewrites a copy for the gateway's
point of view, the same way for both:

* `servers` becomes the gateway URL for the service (`https://<host>/<service>`);
* the gateway's bearer API key becomes the only security scheme;
* the gateway's own errors (401, 429, 502, 504) are added to every operation;
* each operation gets a curl sample generated here. Scalar's generated snippets send an
  array of files as a JSON string, and know nothing about part order.

`compare` lists where a service's document differs from the contract's. Differences are
findings to report, not errors: each implementation documents what it actually does.
"""

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_GATEWAY_PROBLEM = {
    "type": "object",
    "description": "RFC 9457 Problem Details produced by the gateway itself.",
    "properties": {
        "type": {"type": "string", "examples": ["urn:zip-gateway:problem:unauthorized"]},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "code": {"type": "string"},
        "request_id": {"type": "string"},
    },
}


def _gateway_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/GatewayProblem"}}
        },
    }


GATEWAY_RESPONSES = {
    "401": _gateway_response(
        "Gateway: missing or invalid API key (`urn:zip-gateway:problem:unauthorized`)."
    ),
    "429": _gateway_response(
        "Gateway: this account has used its daily byte allowance (`quota-exceeded`); "
        "`Retry-After` gives the seconds until it resets at 00:00 UTC."
    ),
    "502": _gateway_response(
        "Gateway: the service could not be reached or closed the connection "
        "(`upstream-unavailable`, `upstream-error`)."
    ),
    "504": _gateway_response(
        "Gateway: no bytes moved in either direction for 60 s (`upstream-timeout`)."
    ),
}


def for_gateway(spec: dict[str, Any], servers: list[tuple[str, str]], note: str) -> dict[str, Any]:
    """A copy of `spec` as seen through the gateway. `servers` is [(url, description)]."""
    doc = copy.deepcopy(spec)
    doc["servers"] = [{"url": url, "description": text} for url, text in servers]
    doc["security"] = [{"gatewayKey": []}]
    components = doc.setdefault("components", {})
    components["securitySchemes"] = {
        "gatewayKey": {
            "type": "http",
            "scheme": "bearer",
            "description": "The API key issued to you by the gateway operator.",
        }
    }
    components.setdefault("schemas", {})["GatewayProblem"] = _GATEWAY_PROBLEM
    info = doc.setdefault("info", {})
    info["description"] = f"{note}\n\n{info.get('description', '')}".strip()
    # With one server the samples can name it; with several the reader picks one.
    base_url = servers[0][0] if len(servers) == 1 else "$BASE_URL"
    for path, operation, method in operations(doc):
        operation.pop("security", None)
        responses = operation.setdefault("responses", {})
        for status, response in GATEWAY_RESPONSES.items():
            responses.setdefault(status, response)
        operation["x-codeSamples"] = [
            {
                "lang": "Shell",
                "label": "curl",
                "source": curl_sample(doc, base_url, method, path, operation),
            }
        ]
    return doc


def operations(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    """(path, operation, method) for every operation in the document."""
    found: list[tuple[str, dict[str, Any], str]] = []
    for path, item in (spec.get("paths") or {}).items():
        if isinstance(item, dict):
            for method in HTTP_METHODS:
                operation = item.get(method)
                if isinstance(operation, dict):
                    found.append((path, operation, method))
    return found


def resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Follow a local `$ref` (`#/components/...`); anything else is returned as is."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 20:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                return node  # dangling: show it unresolved rather than fail
            target = target[part]
        node, depth = target, depth + 1
    return node


# --------------------------------------------------------------------------- curl samples


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _example(schema: dict[str, Any]) -> Any:
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    return None


def _is_binary(schema: dict[str, Any]) -> bool:
    return schema.get("format") == "binary" or "contentMediaType" in schema


def curl_sample(
    spec: dict[str, Any], base_url: str, method: str, path: str, operation: dict[str, Any]
) -> str:
    def path_value(match: re.Match[str]) -> str:
        for parameter in operation.get("parameters", []):
            parameter = resolve(spec, parameter)
            if parameter.get("in") == "path" and parameter.get("name") == match.group(1):
                value = parameter.get("example") or _example(parameter.get("schema", {}))
                if value is not None:
                    return str(value)
        return f"<{match.group(1)}>"

    url = base_url.rstrip("/") + re.sub(r"\{([^}]+)\}", path_value, path)
    args: list[str] = []
    content = resolve(spec, operation.get("requestBody", {})).get("content", {})
    if "multipart/form-data" in content:
        # Fields before files, in schema order; one -F per file. curl sets the
        # Content-Type, boundary included.
        schema = resolve(spec, content["multipart/form-data"].get("schema", {}))
        fields, files = [], []
        for name, prop in (schema.get("properties") or {}).items():
            prop = resolve(spec, prop)
            if prop.get("type") == "array" and _is_binary(resolve(spec, prop.get("items", {}))):
                files += [f"-F {_quote(f'{name}=@{n}')}" for n in ("a.txt", "photo.jpg")]
            elif _is_binary(prop):
                files.append(f"-F {_quote(f'{name}=@file.bin')}")
            else:
                example = _example(prop)
                value = "" if example is None else str(example)
                fields.append(f"-F {_quote(f'{name}={value}')}")
        args += fields + files
    elif "application/json" in content:
        example = _example(resolve(spec, content["application/json"].get("schema", {})))
        args += [
            "-H 'Content-Type: application/json'",
            f"-d {_quote(json.dumps(example if example is not None else {}))}",
        ]
    # curl infers GET, and POST from -F or -d; anything else needs -X.
    if not (method == "get" or (method == "post" and args)):
        args.insert(0, f"-X {method.upper()}")

    output: list[str] = []
    if "application/zip" in _success_media(spec, operation):
        output = ["-o archive.zip", "-D -"]  # save the archive, print the headers
    lines = [f'curl -sS "{url}"', '-H "Authorization: Bearer $API_KEY"', *output, *args]
    note = "# API_KEY: your gateway API key"
    if base_url == "$BASE_URL":
        note += "; BASE_URL: one of the servers above"
    return note + "\n" + " \\\n  ".join(lines)


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True)
class Difference:
    operation: str  # "POST /api/v1/zip", or "" for document-wide findings
    aspect: str  # what differs, e.g. "response status"
    contract: str  # what the contract says ("" if absent)
    service: str  # what the service documents ("" if absent)


def _key(method: str, path: str) -> str:
    # Path parameter names may differ ({name} vs {doc}); the route is the same.
    route = re.sub(r"\{[^}]+\}", "{}", path)
    return f"{method.upper()} {route}"


def _body(spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    content = resolve(spec, operation.get("requestBody", {})).get("content") or {}
    return {media: resolve(spec, body.get("schema", {})) for media, body in content.items()}


def _success_media(spec: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    media: set[str] = set()
    for status, response in (operation.get("responses") or {}).items():
        if str(status).startswith("2"):
            media |= set(resolve(spec, response).get("content") or {})
    return media


def _show(values: set[str]) -> str:
    return ", ".join(sorted(values))


def compare(contract: dict[str, Any], service: dict[str, Any]) -> list[Difference]:
    """Where `service` documents something different from `contract`."""
    theirs = {_key(m, p): (p, op) for p, op, m in operations(service)}
    ours = {_key(m, p): (p, op) for p, op, m in operations(contract)}
    found: list[Difference] = []
    for key in sorted(ours.keys() - theirs.keys()):
        found.append(Difference(key, "operation", "documented", "missing"))
    for key in sorted(theirs.keys() - ours.keys()):
        found.append(Difference(key, "operation", "", "documented (not in the contract)"))
    for key in sorted(ours.keys() & theirs.keys()):
        c_op, s_op = ours[key][1], theirs[key][1]
        c_status = {str(s) for s in c_op.get("responses") or {}} - {"default"}
        s_status = {str(s) for s in s_op.get("responses") or {}} - {"default"}
        for status in sorted(c_status - s_status):
            found.append(Difference(key, "response status", status, "not documented"))
        for status in sorted(s_status - c_status):
            found.append(Difference(key, "response status", "", status))
        c_body, s_body = _body(contract, c_op), _body(service, s_op)
        if set(c_body) != set(s_body):
            found.append(
                Difference(key, "request media type", _show(set(c_body)), _show(set(s_body)))
            )
        for media in sorted(set(c_body) & set(s_body)):
            c_props = set(c_body[media].get("properties") or {})
            s_props = set(s_body[media].get("properties") or {})
            if c_props != s_props:
                found.append(
                    Difference(key, f"request fields ({media})", _show(c_props), _show(s_props))
                )
            c_req = set(c_body[media].get("required") or [])
            s_req = set(s_body[media].get("required") or [])
            if c_req != s_req:
                found.append(
                    Difference(key, f"required fields ({media})", _show(c_req), _show(s_req))
                )
        c_media, s_media = _success_media(contract, c_op), _success_media(service, s_op)
        if c_media != s_media:
            found.append(Difference(key, "success media type", _show(c_media), _show(s_media)))
    return found
