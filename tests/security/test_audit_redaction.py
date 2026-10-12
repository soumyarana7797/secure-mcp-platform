"""What may and may not reach the audit trail."""

import json

from shared.audit.redact import args_hash, redact_args

SCHEMA = {
    "properties": {
        "competition": {"anyOf": [{"enum": ["PL", "LL"], "type": "string"}, {"type": "null"}]},
        "date_from": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}]},
        "top_k": {"type": "integer"},
        "query": {"type": "string"},
        "api_key": {"type": "string"},
    }
}


def test_values_are_logged_only_when_the_schema_says_they_are_not_free_text() -> None:
    out = redact_args(
        {"competition": "PL", "date_from": "2026-10-11", "top_k": 5, "query": "my salary is 90000"},
        SCHEMA,
    )
    assert out["competition"] == "PL" and out["date_from"] == "2026-10-11" and out["top_k"] == 5
    assert out["query"] == {"type": "string", "len": 18}
    assert "salary" not in json.dumps(out)


def test_sensitive_looking_keys_are_always_redacted_even_if_the_schema_allows_them() -> None:
    schema = {"properties": {"password": {"type": "integer"}}}
    out = redact_args(
        {"password": 1234, "Authorization": "Bearer abc", "client_secret": "x"}, schema
    )
    assert set(out.values()) == {"[REDACTED]"}


def test_unknown_tools_and_unexpected_keys_log_shape_only() -> None:
    out = redact_args({"anything": "secret stuff", "n": 7}, None)
    assert out == {"anything": {"type": "string", "len": 12}, "n": {"type": "int"}}
    assert redact_args({"extra": "x"}, SCHEMA)["extra"] == {"type": "string", "len": 1}


def test_nested_structures_are_summarised_not_copied() -> None:
    out = redact_args({"items": [1, 2, 3], "obj": {"ssn": "123-45-6789"}}, SCHEMA)
    assert out == {"items": {"type": "array", "len": 3}, "obj": {"type": "object", "keys": 1}}
    assert "123-45" not in json.dumps(out)


def test_a_schema_branch_that_allows_free_text_makes_the_whole_field_unsafe() -> None:
    schema = {"properties": {"x": {"anyOf": [{"enum": ["a"]}, {"type": "string"}]}}}
    assert redact_args({"x": "free text"}, schema)["x"] == {"type": "string", "len": 9}


def test_key_count_is_bounded() -> None:
    out = redact_args({f"k{i}": i for i in range(100)}, None)
    assert len(out) == 26 and out["_truncated"] == 75


def test_hash_is_stable_across_key_order_and_changes_with_content() -> None:
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})
    assert args_hash({"a": 1}) != args_hash({"a": 2})
    assert len(args_hash({})) == 64
