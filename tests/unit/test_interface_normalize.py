"""Unit tests for interface_normalize.

Coverage:
- json_schema: properties → fields; required propagates; operations/events
  pass through; malformed JSON → 422.
- openapi 3.x: paths → operations; operationId / fallback name; params
  collect path+query+body; return type extracted from 2xx response;
  swagger 2 / non-3.x → 422.
- typescript (restricted subset: type/interface declarations, primitive fields only):
    - ``type X = { id: string; active: boolean }`` → InterfaceSurface
      with two fields.
    - Primitive arrays (``tags: string[]``) accepted.
    - Optional fields (``name?: string``) marked required=False.
    - Generics / unions / methods / nested objects → 422.
- normalize() rejects unknown interface_format.
"""

from __future__ import annotations

import json

import pytest

from registry.exceptions import ValidationError
from registry.service.interface_normalize import VALID_FORMATS, normalize
from registry.types import InterfaceSurface

# ---------------------------------------------------------------------------
# normalize() dispatch
# ---------------------------------------------------------------------------


def test_normalize_rejects_unknown_format() -> None:
    with pytest.raises(ValidationError):
        normalize({"type": "object"}, "graphql")  # type: ignore[arg-type]


def test_valid_formats_set_is_the_documented_three() -> None:
    assert VALID_FORMATS == {"json_schema", "typescript", "openapi"}


# ---------------------------------------------------------------------------
# json_schema
# ---------------------------------------------------------------------------


class TestJsonSchema:
    def test_object_with_properties_yields_fields(self) -> None:
        schema = {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
                "active": {"type": "boolean"},
            },
        }
        surf = normalize(schema, "json_schema")
        assert isinstance(surf, InterfaceSurface)
        names = {f["name"] for f in surf.fields}
        assert names == {"id", "active"}
        id_field = next(f for f in surf.fields if f["name"] == "id")
        active_field = next(f for f in surf.fields if f["name"] == "active")
        assert id_field["required"] is True
        assert active_field["required"] is False

    def test_top_level_operations_pass_through(self) -> None:
        schema = {
            "type": "object",
            "operations": [{"name": "ping", "method": "GET"}],
        }
        surf = normalize(schema, "json_schema")
        assert surf.operations == [{"name": "ping", "method": "GET"}]
        assert surf.fields == []

    def test_string_input_is_decoded(self) -> None:
        surf = normalize(
            json.dumps({"type": "object", "properties": {"x": {"type": "string"}}}),
            "json_schema",
        )
        assert surf.fields[0]["name"] == "x"

    def test_malformed_json_raises_422(self) -> None:
        with pytest.raises(ValidationError):
            normalize("not-json", "json_schema")

    def test_non_object_decoded_raises_422(self) -> None:
        with pytest.raises(ValidationError):
            normalize("[1,2,3]", "json_schema")

    def test_unknown_property_type_defaults_to_unknown(self) -> None:
        schema = {
            "type": "object",
            "properties": {"weird": {}},
        }
        surf = normalize(schema, "json_schema")
        assert surf.fields[0]["type"] == "unknown"


# ---------------------------------------------------------------------------
# openapi 3.x
# ---------------------------------------------------------------------------


class TestOpenApi:
    def test_minimal_openapi_extracts_operations(self) -> None:
        doc = {
            "openapi": "3.0.3",
            "paths": {
                "/payments": {
                    "post": {
                        "operationId": "createPayment",
                        "parameters": [
                            {
                                "name": "X-Idempotency-Key",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}},
                    }
                }
            },
        }
        surf = normalize(doc, "openapi")
        assert len(surf.operations) == 1
        op = surf.operations[0]
        assert op["name"] == "createPayment"
        assert op["method"] == "POST"
        assert op["path"] == "/payments"
        # Header param + body param both included.
        assert len(op["params"]) == 2
        assert any(p["in"] == "body" for p in op["params"])
        assert op["returns"] == "object"

    def test_missing_operation_id_uses_method_path_fallback(self) -> None:
        doc = {
            "openapi": "3.1.0",
            "paths": {"/health": {"get": {"responses": {}}}},
        }
        surf = normalize(doc, "openapi")
        assert surf.operations[0]["name"] == "GET /health"

    def test_swagger_2_is_rejected(self) -> None:
        doc = {"swagger": "2.0", "paths": {}}
        with pytest.raises(ValidationError):
            normalize(doc, "openapi")

    def test_non_object_paths_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize({"openapi": "3.0.0", "paths": []}, "openapi")

    def test_string_input_is_decoded(self) -> None:
        surf = normalize(
            json.dumps(
                {
                    "openapi": "3.0.0",
                    "paths": {"/p": {"get": {"operationId": "p", "responses": {}}}},
                }
            ),
            "openapi",
        )
        assert surf.operations[0]["name"] == "p"


# ---------------------------------------------------------------------------
# typescript (restricted subset)
# ---------------------------------------------------------------------------


class TestTypeScript:
    def test_simple_type_alias(self) -> None:
        src = "type Foo = { id: string; active: boolean; }"
        surf = normalize(src, "typescript")
        names = {f["name"] for f in surf.fields}
        assert names == {"id", "active"}
        assert all(f["required"] is True for f in surf.fields)
        types = {f["name"]: f["type"] for f in surf.fields}
        assert types == {"id": "string", "active": "boolean"}

    def test_simple_interface(self) -> None:
        src = "interface Bar { count: number; tags: string[]; }"
        surf = normalize(src, "typescript")
        types = {f["name"]: f["type"] for f in surf.fields}
        assert types == {"count": "number", "tags": "string[]"}

    def test_optional_field_marked_not_required(self) -> None:
        src = "type X = { name?: string; }"
        surf = normalize(src, "typescript")
        assert surf.fields[0]["required"] is False

    def test_dict_wrapper_with_source_key(self) -> None:
        surf = normalize({"source": "type X = { x: number; }"}, "typescript")
        assert surf.fields[0] == {"name": "x", "type": "number", "required": True}

    def test_generic_rejected(self) -> None:
        src = "type Box<T> = { value: T; }"
        with pytest.raises(ValidationError) as excinfo:
            normalize(src, "typescript")
        assert "TypeScript" in str(excinfo.value)

    def test_union_rejected(self) -> None:
        src = "type X = { v: string | number; }"
        with pytest.raises(ValidationError):
            normalize(src, "typescript")

    def test_method_signature_rejected(self) -> None:
        src = "interface I { greet(name: string): string; }"
        with pytest.raises(ValidationError):
            normalize(src, "typescript")

    def test_nested_object_rejected(self) -> None:
        src = "type X = { inner: { y: string; }; }"
        with pytest.raises(ValidationError):
            normalize(src, "typescript")

    def test_stray_top_level_code_rejected(self) -> None:
        # A const declaration outside a type/interface block must be rejected.
        src = "type X = { x: string; }\nconst k = 1;"
        with pytest.raises(ValidationError):
            normalize(src, "typescript")

    def test_empty_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            normalize("", "typescript")

    def test_non_string_input_rejected(self) -> None:
        with pytest.raises(ValidationError):
            normalize(123, "typescript")  # type: ignore[arg-type]
