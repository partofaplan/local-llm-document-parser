"""Prompt-driven structured extraction over parsed document text."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .llm import OllamaClient
from .prompts import FieldSpec, PromptSpec

log = logging.getLogger(__name__)

USER_TEMPLATE = """{instructions}

Fields to extract:
{fields}

Rules:
- Only use information stated in the document. Use null (or an empty list) when a value is not present.
- Copy names, numbers and identifiers exactly. Write dates as YYYY-MM-DD and numbers without currency symbols or thousands separators.
- Respond with a single JSON object matching the required schema.
{part_note}
<document>
{document}
</document>"""


@dataclass
class ExtractionResult:
    prompt: str
    model: str
    data: dict[str, Any]
    chunks: int
    truncated: bool
    duration_s: float
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "model": self.model,
            "data": self.data,
            "chunks": self.chunks,
            "truncated": self.truncated,
            "duration_s": round(self.duration_s, 2),
            "validation_errors": self.validation_errors,
        }


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into ~size character chunks, preferring paragraph then line boundaries."""
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            if cut > size // 2:
                end = start + cut
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def merge_values(spec: FieldSpec, values: list[Any]) -> Any:
    present = [v for v in values if not _is_empty(v)]
    if spec.type == "array":
        merged, seen = [], set()
        for v in present:
            for item in v if isinstance(v, list) else [v]:
                key = json.dumps(item, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    merged.append(item)
        return merged
    if spec.type == "object":
        objs = [v for v in present if isinstance(v, dict)]
        if not objs:
            return None
        return {f.name: merge_values(f, [o.get(f.name) for o in objs]) for f in spec.fields or []}
    return present[0] if present else None  # first chunk that mentions a scalar wins


def merge_results(spec: PromptSpec, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {f.name: merge_values(f, [r.get(f.name) for r in results]) for f in spec.fields}


def validate(spec: PromptSpec, data: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(spec.json_schema())
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = ".".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    for f in spec.fields:
        if f.required and _is_empty(data.get(f.name)):
            errors.append(f"{f.name}: required field not found in document")
    return errors


def build_user_prompt(spec: PromptSpec, document: str, part: int = 1, parts: int = 1) -> str:
    note = ""
    if parts > 1:
        note = (
            f"- This is part {part} of {parts} of a longer document. Extract what appears in this part; "
            "use null for anything not in it.\n"
        )
    return USER_TEMPLATE.format(
        instructions=spec.instructions.strip(),
        fields=spec.fields_description(),
        part_note=note,
        document=document,
    )


async def extract(
    client: OllamaClient,
    spec: PromptSpec,
    document_text: str,
    *,
    default_model: str,
    chunk_chars: int,
    chunk_overlap: int,
    max_chunks: int,
    base_options: dict[str, Any],
) -> ExtractionResult:
    started = time.monotonic()
    model = spec.model or default_model
    chunks = chunk_text(document_text, chunk_chars, chunk_overlap)
    truncated = len(chunks) > max_chunks
    chunks = chunks[:max_chunks]
    options = {**base_options, **spec.options}
    schema = spec.json_schema()

    results = []
    for i, chunk in enumerate(chunks, 1):
        log.info("prompt=%s model=%s chunk %d/%d (%d chars)", spec.name, model, i, len(chunks), len(chunk))
        results.append(
            await client.chat_json(
                model=model,
                system=spec.system,
                user=build_user_prompt(spec, chunk, i, len(chunks)),
                schema=schema,
                options=options,
            )
        )

    data = results[0] if len(results) == 1 else merge_results(spec, results)
    # Keep output shape stable even if the model dropped a key.
    data = {f.name: data.get(f.name, [] if f.type == "array" else None) for f in spec.fields}
    return ExtractionResult(
        prompt=spec.name,
        model=model,
        data=data,
        chunks=len(chunks),
        truncated=truncated,
        duration_s=time.monotonic() - started,
        validation_errors=validate(spec, data),
    )
