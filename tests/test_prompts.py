import pytest
from pydantic import ValidationError

from app.prompts import PromptRegistry, PromptSpec
from conftest import PROMPTS


def test_bundled_prompts_load_cleanly():
    prompts, errors = PromptRegistry(PROMPTS).load()
    assert errors == {}
    assert {"invoice", "contract", "document-summary"} <= set(prompts)
    assert prompts["invoice"].template_path().is_file()


def test_schema_makes_optional_fields_nullable():
    spec = PromptSpec.model_validate({
        "name": "t",
        "fields": [
            {"name": "a", "required": True},
            {"name": "b", "type": "number"},
            {"name": "c", "enum": ["x", "y"]},
            {"name": "d", "type": "array", "items": {"type": "object", "fields": [{"name": "e"}]}},
        ],
    })
    schema = spec.json_schema()
    assert schema["required"] == ["a", "b", "c", "d"]
    assert schema["properties"]["a"]["type"] == "string"
    assert schema["properties"]["b"]["type"] == ["number", "null"]
    assert schema["properties"]["c"]["enum"] == ["x", "y", None]
    assert schema["properties"]["d"]["type"] == "array"
    assert schema["properties"]["d"]["items"]["properties"]["e"]["type"] == ["string", "null"]


def test_invalid_prompt_reported_not_raised(tmp_path):
    (tmp_path / "good.yaml").write_text("fields: [{name: x}]\n")
    (tmp_path / "bad.yaml").write_text("fields: [{name: 'not valid'}]\n")
    (tmp_path / "notes.txt").write_text("ignored")
    prompts, errors = PromptRegistry(tmp_path).load()
    assert list(prompts) == ["good"]
    assert list(errors) == ["bad.yaml"]


def test_object_requires_fields():
    with pytest.raises(ValidationError):
        PromptSpec.model_validate({"name": "t", "fields": [{"name": "o", "type": "object"}]})
