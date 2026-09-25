"""Prompt files: YAML documents describing what to extract and (optionally) which PDF form to fill.

See prompts/README.md for the file format.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

log = logging.getLogger(__name__)

FieldType = Literal["string", "number", "integer", "boolean", "date", "array", "object"]
IDENT = r"^[A-Za-z_][A-Za-z0-9_]*$"


class FieldSpec(BaseModel):
    name: str = Field("item", pattern=IDENT)
    type: FieldType = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    items: FieldSpec | None = None  # element spec when type == array
    fields: list[FieldSpec] | None = None  # member specs when type == object

    @model_validator(mode="after")
    def _check_shape(self) -> FieldSpec:
        if self.type == "object" and not self.fields:
            raise ValueError(f"field '{self.name}': type object requires 'fields'")
        if self.type == "array" and self.items is None:
            self.items = FieldSpec(type="string")
        return self

    def json_schema(self, nullable: bool | None = None) -> dict[str, Any]:
        nullable = (not self.required) if nullable is None else nullable
        if self.type == "array":
            schema: dict[str, Any] = {"type": "array", "items": self.items.json_schema(nullable=False)}
        elif self.type == "object":
            schema = object_schema(self.fields or [])
        elif self.type == "date":
            schema = {"type": "string", "format": "date"}
        else:
            schema = {"type": self.type}
        if self.enum and self.type == "string":
            schema["enum"] = list(self.enum)
        if self.description:
            schema["description"] = self.description
        if nullable and self.type != "array":
            schema["type"] = [schema["type"], "null"]
            if "enum" in schema:
                schema["enum"] = [*schema["enum"], None]
        return schema

    def describe(self, indent: int = 0) -> str:
        """Human-readable field list embedded in the LLM prompt."""
        pad = "  " * indent
        kind = "date (YYYY-MM-DD)" if self.type == "date" else self.type
        line = f"{pad}- {self.name} ({kind}{', required' if self.required else ''})"
        if self.description:
            line += f": {self.description}"
        if self.enum:
            line += f" One of: {', '.join(self.enum)}."
        lines = [line]
        children = self.fields if self.type == "object" else (
            self.items.fields if self.type == "array" and self.items and self.items.type == "object" else None
        )
        for child in children or []:
            lines.append(child.describe(indent + 1))
        return "\n".join(lines)


def object_schema(fields: list[FieldSpec]) -> dict[str, Any]:
    # Every key is required so the model always emits it; optional fields are nullable instead.
    return {
        "type": "object",
        "properties": {f.name: f.json_schema() for f in fields},
        "required": [f.name for f in fields],
        "additionalProperties": False,
    }


class PdfOutput(BaseModel):
    template: str  # path to a fillable PDF, relative to the prompt file
    # PDF form field name -> dotted path into the result ("vendor.name", "line_items.0.amount")
    # or a format string ("{vendor.name} ({invoice_number})"). Unmapped form fields are matched
    # to result keys by normalised name.
    field_map: dict[str, str] = Field(default_factory=dict)


class PromptSpec(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""
    model: str | None = None
    system: str = (
        "You are a meticulous document analyst. You extract information from documents "
        "exactly as written and never invent values."
    )
    instructions: str = "Extract the requested fields from the document."
    fields: list[FieldSpec]
    options: dict[str, Any] = Field(default_factory=dict)  # extra Ollama options, e.g. num_ctx
    output_pdf: PdfOutput | None = None

    _source: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _unique_names(self) -> PromptSpec:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        return self

    @property
    def source(self) -> Path | None:
        return self._source

    def json_schema(self) -> dict[str, Any]:
        return object_schema(self.fields)

    def fields_description(self) -> str:
        return "\n".join(f.describe() for f in self.fields)

    def template_path(self) -> Path | None:
        if not self.output_pdf:
            return None
        path = Path(self.output_pdf.template)
        if not path.is_absolute() and self._source is not None:
            path = self._source.parent / path
        return path

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "fields": [f.name for f in self.fields],
            "fills_pdf": self.output_pdf is not None,
        }


class PromptNotFound(KeyError):
    pass


class PromptRegistry:
    """Loads prompt files from a directory on every call so ConfigMap updates apply without a restart."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def load(self) -> tuple[dict[str, PromptSpec], dict[str, str]]:
        prompts: dict[str, PromptSpec] = {}
        errors: dict[str, str] = {}
        if not self.directory.is_dir():
            return prompts, {str(self.directory): "prompt directory not found"}
        for path in sorted(self.directory.iterdir()):
            if path.suffix not in (".yaml", ".yml") or path.name.startswith(".") or path.stem == "kustomization":
                continue
            try:
                spec = load_prompt_file(path)
            except Exception as exc:
                log.warning("Invalid prompt file %s: %s", path, exc)
                errors[path.name] = str(exc)
                continue
            if spec.name in prompts:
                errors[path.name] = f"duplicate prompt name '{spec.name}'"
                continue
            prompts[spec.name] = spec
        return prompts, errors

    def get(self, name: str) -> PromptSpec:
        prompts, _ = self.load()
        try:
            return prompts[name]
        except KeyError:
            raise PromptNotFound(name) from None


def load_prompt_file(path: Path) -> PromptSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prompt file must be a YAML mapping")
    raw.setdefault("name", path.stem)
    spec = PromptSpec.model_validate(raw)
    spec._source = path
    return spec
