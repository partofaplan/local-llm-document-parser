"""Command line entry point for batch use without the HTTP server.

    python -m app.cli prompts
    python -m app.cli parse invoice.pdf
    python -m app.cli extract invoice.pdf --prompt invoice
    python -m app.cli fill invoice.pdf --prompt invoice -o filled.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import get_settings
from .extractor import extract
from .filler import fill_pdf
from .llm import OllamaClient
from .pdf_parser import parse_pdf
from .prompts import PromptRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docparser")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prompts", help="list available prompt files")
    p_parse = sub.add_parser("parse", help="print extracted PDF text")
    p_parse.add_argument("pdf", type=Path)
    for name in ("extract", "fill"):
        p = sub.add_parser(name)
        p.add_argument("pdf", type=Path)
        p.add_argument("--prompt", required=True)
        if name == "fill":
            p.add_argument("--template", type=Path, help="fillable PDF (defaults to the prompt's template)")
            p.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)

    s = get_settings()
    registry = PromptRegistry(s.prompts_dir)
    if args.cmd == "prompts":
        prompts, errors = registry.load()
        print(json.dumps({"prompts": [p.summary() for p in prompts.values()], "errors": errors}, indent=2))
        return 0

    doc = parse_pdf(args.pdf.read_bytes(), max_pages=s.max_pages, ocr_enabled=s.ocr_enabled,
                    ocr_min_chars=s.ocr_min_chars, ocr_dpi=s.ocr_dpi, ocr_lang=s.ocr_lang)
    if args.cmd == "parse":
        print(doc.full_text())
        return 0

    spec = registry.get(args.prompt)

    async def run():
        client = OllamaClient(s.ollama_url, s.request_timeout_s)
        try:
            return await extract(client, spec, doc.full_text(), default_model=s.model, chunk_chars=s.chunk_chars,
                                 chunk_overlap=s.chunk_overlap, max_chunks=s.max_chunks,
                                 base_options={"temperature": s.temperature, "num_ctx": s.num_ctx})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    if args.cmd == "fill":
        template = args.template or spec.template_path()
        if template is None:
            print("error: prompt has no output_pdf template; pass --template", file=sys.stderr)
            return 2
        pdf, _ = fill_pdf(template, result.data, spec.output_pdf.field_map if spec.output_pdf else {})
        args.output.write_bytes(pdf)
        print(f"wrote {args.output}", file=sys.stderr)
    return 1 if result.validation_errors else 0


if __name__ == "__main__":
    sys.exit(main())
