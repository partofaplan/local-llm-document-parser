"""Fill AcroForm PDF templates with extracted data."""

from __future__ import annotations

import io
import re
import textwrap
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.]+)\}")
_MULTILINE = 1 << 12  # /Ff bit for multiline text fields


class FillError(ValueError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve(data: Any, path: str) -> Any:
    """Look up a dotted path such as 'vendor.name' or 'line_items.0.amount'."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return "; ".join(to_text(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {to_text(v)}" for k, v in value.items() if v not in (None, "", []))
    return str(value)


def render(mapping: str, data: dict[str, Any]) -> Any:
    if "{" in mapping:
        return _PLACEHOLDER.sub(lambda m: to_text(resolve(data, m.group(1))), mapping)
    return resolve(data, mapping)


def _load(template: bytes | Path) -> PdfReader:
    try:
        return PdfReader(io.BytesIO(template) if isinstance(template, bytes) else str(template))
    except Exception as exc:
        raise FillError(f"Unable to read template PDF: {exc}") from exc


def form_fields(template: bytes | Path) -> dict[str, dict[str, Any]]:
    reader = _load(template)
    fields = reader.get_fields() or {}
    out = {}
    for name, f in fields.items():
        if f.get("/FT") is None:  # non-terminal parent node
            continue
        states = f.get("/_States_", [])
        out[name] = {
            "type": {"/Tx": "text", "/Btn": "button", "/Ch": "choice", "/Sig": "signature"}.get(f.get("/FT"), str(f.get("/FT"))),
            "value": str(f.get("/V")) if f.get("/V") is not None else None,
            "states": [str(s) for s in states] if states else None,
        }
    return out


def _checkbox_value(field: dict[str, Any], value: Any) -> str:
    on_states = [s for s in (field.get("states") or []) if s != "/Off"]
    truthy = value if isinstance(value, bool) else str(value).strip().lower() in {"yes", "true", "y", "1", "x", "on"}
    return (on_states[0] if on_states else "/Yes") if truthy else "/Off"


def fill_pdf(template: bytes | Path, data: dict[str, Any], field_map: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
    """Return (filled PDF bytes, {form field: value written})."""
    fields = form_fields(template)
    if not fields:
        raise FillError("Template PDF has no fillable form fields")

    field_map = dict(field_map or {})
    by_norm = {_norm(k): k for k in data}
    for name in fields:
        if name not in field_map and _norm(name) in by_norm:
            field_map[name] = by_norm[_norm(name)]

    values: dict[str, str] = {}
    for pdf_field, mapping in field_map.items():
        if pdf_field not in fields:
            raise FillError(f"field_map references unknown form field '{pdf_field}'")
        value = render(mapping, data)
        if fields[pdf_field]["type"] == "button":
            values[pdf_field] = _checkbox_value(fields[pdf_field], value)
        elif isinstance(value, list):
            values[pdf_field] = "\n".join(to_text(v) for v in value)  # one list item per line
        else:
            values[pdf_field] = to_text(value)

    writer = PdfWriter(clone_from=_load(template))
    # pypdf does not wrap text when it generates appearances, so wrap multiline fields ourselves.
    for name, width in _multiline_widths(writer).items():
        if values.get(name):
            values[name] = "\n".join(textwrap.fill(line, width) or line for line in values[name].split("\n"))
    for page in writer.pages:
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    # Ask viewers to regenerate appearances so text renders consistently everywhere.
    writer.set_need_appearances_writer(True)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), values


def _multiline_widths(writer: PdfWriter) -> dict[str, int]:
    """Approximate characters per line for each multiline text field, from widget width and font size."""
    out: dict[str, int] = {}
    default_da = str(writer._root_object["/AcroForm"].get("/DA", ""))
    for page in writer.pages:
        for ref in page.get("/Annots") or []:
            widget = ref.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            field = widget if "/T" in widget else widget.get("/Parent", {}).get_object()
            if field.get("/FT", widget.get("/FT")) != "/Tx" or not int(field.get("/Ff", 0)) & _MULTILINE:
                continue
            da = str(widget.get("/DA") or field.get("/DA") or default_da)
            match = re.search(r"([\d.]+)\s+Tf", da)
            size = float(match.group(1)) if match and float(match.group(1)) > 0 else 12.0
            x0, _, x1, _ = (float(v) for v in widget["/Rect"])
            out[str(field["/T"])] = max(10, int((abs(x1 - x0) - 4) / (size * 0.5)))  # ~0.5em per glyph
    return out
