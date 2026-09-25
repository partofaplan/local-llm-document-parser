"""HTTP API: parse PDFs, extract structured data with prompt files, fill PDF forms."""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import __version__
from .config import Settings, get_settings
from .extractor import ExtractionResult, extract
from .filler import FillError, fill_pdf, form_fields
from .llm import LLMError, OllamaClient
from .pdf_parser import ParsedDocument, PDFParseError, parse_pdf
from .prompts import PromptNotFound, PromptRegistry, PromptSpec

log = logging.getLogger("docparser")
STATIC = Path(__file__).parent / "static"


class _SkipProbes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(p in record.getMessage() for p in ('"GET /healthz', '"GET /readyz'))


class State:
    def __init__(self, settings: Settings, client: OllamaClient):
        self.settings = settings
        self.client = client
        self.registry = PromptRegistry(settings.prompts_dir)
        self.jobs = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.pull_error: str | None = None
        self.pull_task: asyncio.Task | None = None


async def ensure_models(state: State) -> None:
    """Pull the default model and any model named in a prompt file, retrying until Ollama is up."""
    prompts, _ = state.registry.load()
    models = {state.settings.model, *(p.model for p in prompts.values() if p.model)}
    for model in sorted(models):
        delay = 2.0
        while True:
            try:
                if not await state.client.has_model(model):
                    await state.client.pull(model)
                break
            except Exception as exc:
                state.pull_error = f"{model}: {exc}"
                log.warning("Model %s not ready yet (%s); retrying in %.0fs", model, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
    state.pull_error = None


def create_app(settings: Settings | None = None, client: OllamaClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_SkipProbes())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = State(settings, client or OllamaClient(settings.ollama_url, settings.request_timeout_s))
        app.state.s = state
        if settings.auto_pull:
            state.pull_task = asyncio.create_task(ensure_models(state))
        yield
        if state.pull_task:
            state.pull_task.cancel()
        await state.client.aclose()

    app = FastAPI(title="Local LLM Document Parser", version=__version__, lifespan=lifespan)

    def st(request: Request) -> State:
        return request.app.state.s

    async def read_pdf(upload: UploadFile, limit_mb: int) -> bytes:
        data = await upload.read(limit_mb * 1024 * 1024 + 1)
        if len(data) > limit_mb * 1024 * 1024:
            raise HTTPException(413, f"File exceeds {limit_mb} MB limit")
        if not data:
            raise HTTPException(400, f"'{upload.filename}' is empty")
        return data

    async def parse_upload(state: State, upload: UploadFile) -> ParsedDocument:
        data = await read_pdf(upload, state.settings.max_upload_mb)
        s = state.settings
        try:
            # pdfplumber/tesseract are CPU bound; keep the event loop responsive.
            return await asyncio.to_thread(
                parse_pdf, data, max_pages=s.max_pages, ocr_enabled=s.ocr_enabled,
                ocr_min_chars=s.ocr_min_chars, ocr_dpi=s.ocr_dpi, ocr_lang=s.ocr_lang,
            )
        except PDFParseError as exc:
            raise HTTPException(422, str(exc)) from exc

    def get_prompt(state: State, name: str) -> PromptSpec:
        try:
            return state.registry.get(name)
        except PromptNotFound:
            raise HTTPException(404, f"Unknown prompt '{name}'") from None

    async def run_extraction(state: State, spec: PromptSpec, doc: ParsedDocument) -> ExtractionResult:
        text = doc.full_text()
        if not text.strip():
            raise HTTPException(422, "No text could be extracted from the PDF")
        s = state.settings
        async with state.jobs:
            try:
                result = await extract(
                    state.client, spec, text,
                    default_model=s.model, chunk_chars=s.chunk_chars, chunk_overlap=s.chunk_overlap,
                    max_chunks=s.max_chunks,
                    base_options={"temperature": s.temperature, "num_ctx": s.num_ctx},
                )
            except LLMError as exc:
                raise HTTPException(502, str(exc)) from exc
        result.truncated = result.truncated or doc.truncated
        return result

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        state = st(request)
        try:
            ready = await state.client.has_model(state.settings.model)
            detail = None if ready else state.pull_error or f"model {state.settings.model} not yet available"
        except Exception as exc:
            ready, detail = False, f"ollama unreachable: {exc}"
        body = {"ready": ready, "model": state.settings.model, "detail": detail}
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/api/prompts")
    async def list_prompts(request: Request) -> dict[str, Any]:
        prompts, errors = st(request).registry.load()
        return {"prompts": [p.summary() for p in prompts.values()], "errors": errors}

    @app.get("/api/prompts/{name}")
    async def prompt_detail(request: Request, name: str) -> dict[str, Any]:
        spec = get_prompt(st(request), name)
        return {**spec.model_dump(exclude={"fields"}), **spec.summary(), "schema": spec.json_schema()}

    @app.post("/api/parse")
    async def parse(request: Request, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        doc = await parse_upload(st(request), file)
        return {"filename": file.filename, **doc.to_dict(), "text": doc.full_text()}

    @app.post("/api/extract")
    async def extract_endpoint(
        request: Request,
        file: Annotated[UploadFile, File()],
        prompt: Annotated[str, Form()],
        include_text: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        state = st(request)
        spec = get_prompt(state, prompt)
        doc = await parse_upload(state, file)
        result = await run_extraction(state, spec, doc)
        body = {"filename": file.filename, **result.to_dict()}
        if include_text:
            body["text"] = doc.full_text()
        return body

    @app.post("/api/fill", response_model=None)
    async def fill(
        request: Request,
        file: Annotated[UploadFile, File(description="Source PDF to read information from")],
        prompt: Annotated[str, Form()],
        template: Annotated[UploadFile | None, File(description="Fillable PDF; overrides the prompt's template")] = None,
        format: Annotated[str, Query(pattern="^(pdf|json)$")] = "pdf",
    ) -> Response | dict[str, Any]:
        state = st(request)
        spec = get_prompt(state, prompt)
        if template is not None and template.filename:
            template_src: bytes | Path = await read_pdf(template, state.settings.max_upload_mb)
        elif (path := spec.template_path()) is not None:
            if not path.is_file():
                raise HTTPException(500, f"Template for prompt '{spec.name}' not found: {path.name}")
            template_src = path
        else:
            raise HTTPException(400, f"Prompt '{spec.name}' has no output_pdf template; upload one as 'template'")

        doc = await parse_upload(state, file)
        result = await run_extraction(state, spec, doc)
        field_map = spec.output_pdf.field_map if spec.output_pdf else {}
        try:
            pdf, written = fill_pdf(template_src, result.data, field_map)
        except FillError as exc:
            raise HTTPException(422, str(exc)) from exc

        if format == "json":
            return {**result.to_dict(), "filled_fields": written, "pdf_base64": base64.b64encode(pdf).decode()}
        stem = Path(file.filename or "document").stem
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{stem}-{spec.name}-filled.pdf"',
                "X-Validation-Errors": str(len(result.validation_errors)),
            },
        )

    @app.post("/api/templates/inspect")
    async def inspect_template(request: Request, template: Annotated[UploadFile, File()]) -> dict[str, Any]:
        data = await read_pdf(template, st(request).settings.max_upload_mb)
        try:
            return {"filename": template.filename, "fields": form_fields(data)}
        except FillError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


app = create_app()
