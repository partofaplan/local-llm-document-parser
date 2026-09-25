# Local LLM Document Parser

A self-contained service that reads PDFs and uses a local LLM to extract structured data and fill PDF
forms. The extraction for each task is defined by a YAML prompt file. Nothing leaves your
infrastructure: the model runs in [Ollama](https://ollama.com) next to the API.

```
            ┌──────────────── namespace: docparser ────────────────┐
 PDF  ──▶   │  docparser-api (FastAPI)            ollama            │
 upload     │   ├─ pdfplumber text + tables  ──▶  qwen2.5:3b (CPU)  │
            │   ├─ tesseract OCR (scans)         or any model /GPU  │
            │   ├─ prompt files (ConfigMap)      PVC: model cache   │
            │   └─ pypdf form filling                               │
            └───────────────────────────────────────────────────────┘
```

**Pipeline:** parse the PDF (OCR any page with no text layer) → pick a prompt file → the LLM returns
JSON constrained to the schema built from the prompt's fields → validate the JSON → optionally write
it into a fillable PDF template.

## Quick start on the picard cluster

Prerequisites: Docker Desktop running and the `picard` k3d cluster started.

```bash
make venv test          # optional: unit tests
make picard-deploy      # build, import the image into k3d, apply, wait for the model pull
make picard-smoke       # extract + fill samples/sample_invoice.pdf
```

Then open **http://docparser.localhost:8080**. The first deploy downloads the Ollama image (about
2.8 GB) and the model (about 1.9 GB). After that, the model is cached on a PVC.

| Command | Purpose |
| --- | --- |
| `make picard-status` | Pods, services and readiness |
| `make picard-logs` | Tail the API logs |
| `make picard-forward` | Port-forward to `localhost:8081`, bypassing the ingress |
| `make picard-delete` | Remove everything, including the model volume |

## API

| Endpoint | Description |
| --- | --- |
| `GET /` | Test UI: upload a PDF, pick a prompt, extract or fill |
| `POST /api/parse` | `file` → per-page text, tables (as Markdown), metadata |
| `POST /api/extract` | `file`, `prompt` → `{data, validation_errors, ...}` |
| `POST /api/fill` | `file`, `prompt`, optional `template` → filled PDF (`?format=json` returns data plus base64 PDF) |
| `POST /api/templates/inspect` | `template` → form field names and types, for writing `field_map` |
| `GET /api/prompts[/{name}]` | Available prompts and their generated JSON schema |
| `GET /healthz`, `GET /readyz` | Liveness; readiness (Ollama reachable and model present) |

```bash
curl -F file=@samples/sample_contract.pdf -F prompt=contract http://docparser.localhost:8080/api/extract
curl -F file=@samples/sample_invoice.pdf  -F prompt=invoice  -o filled.pdf http://docparser.localhost:8080/api/fill
```

## Prompt files

Prompt files live in [prompts/](prompts/). See [prompts/README.md](prompts/README.md) for the format.
The bundled prompts are:

- **`invoice`:** vendor, customer, totals and line items. Fills `invoice_summary.pdf`.
- **`contract`:** parties, dates, renewal terms, obligations and a risk level.
- **`document-summary`:** a generic summary for any document.

To add a prompt, drop a YAML file into `prompts/`, list it in `prompts/kustomization.yaml`, and
re-apply. No image rebuild is needed.

## Running on other Linux infrastructure

The API image is `python:3.12-slim` plus tesseract. Both it and the Ollama image are multi-arch
(`linux/amd64`, `linux/arm64`).

**Any Kubernetes cluster:**

```bash
make buildx REGISTRY=registry.example.com TAG=0.1.0
# edit deploy/k8s/overlays/production/kustomization.yaml (image) and ingress.yaml (host)
kubectl apply -k deploy/k8s/overlays/production
```

The production overlay can enable two optional components:

- **`components/gpu`** requests `nvidia.com/gpu: 1`, tolerates GPU taints, and switches to `qwen2.5:7b` with a 16k context. It needs the NVIDIA device plugin or GPU operator.
- **`components/airgapped`** is for clusters without internet access. It uses an Ollama image with the model baked in and disables runtime pulls. Build that image with:

  ```bash
  docker buildx build -f Dockerfile.ollama --build-arg MODEL=qwen2.5:3b \
    --platform linux/amd64,linux/arm64 -t registry.example.com/docparser-ollama:qwen2.5-3b --push .
  ```

  You also need to mirror the API image into your registry.

**A single Linux host with Docker:**

```bash
docker compose up -d --build                                            # CPU
docker compose -f docker-compose.yaml -f docker-compose.gpu.yaml up -d  # NVIDIA GPU
```

**No containers:** Python 3.11+, `tesseract-ocr` and a local Ollama are enough:

```bash
pip install -r requirements.txt
python -m app.cli extract invoice.pdf --prompt invoice
python -m app.cli fill invoice.pdf --prompt invoice -o filled.pdf
uvicorn app.main:app --port 8080
```

## Configuration

All settings are `DOCPARSER_*` environment variables, set in the `docparser-config` ConfigMap. Defaults
are in [app/config.py](app/config.py).

| Variable | Default | Notes |
| --- | --- | --- |
| `DOCPARSER_MODEL` | `qwen2.5:3b` | Any Ollama model. `qwen2.5:7b` / `llama3.1:8b` are more accurate but need about 6 GB RAM or a GPU. |
| `DOCPARSER_AUTO_PULL` | `true` | Pull missing models on startup |
| `DOCPARSER_NUM_CTX` | `8192` | Model context window (tokens) |
| `DOCPARSER_CHUNK_CHARS` / `MAX_CHUNKS` | `12000` / `8` | Long documents are split into chunks, each extracted separately, and the results merged |
| `DOCPARSER_MAX_CONCURRENT_JOBS` | `2` | LLM calls allowed at once per API pod |
| `DOCPARSER_OCR_ENABLED` / `OCR_LANG` | `true` / `eng` | Other languages need `tesseract-ocr-<lang>` added to the Dockerfile |
| `DOCPARSER_MAX_UPLOAD_MB` / `MAX_PAGES` | `50` / `200` | Input limits |

## Sizing

On CPU, `qwen2.5:3b` takes 10–25 s for a one-page document on picard and needs about 3 GB RAM. The
picard overlay limits Ollama to 4.5 GiB because the Docker Desktop VM has about 7.6 GiB for all nodes.
For larger models, raise Docker Desktop's memory or use a GPU node. Ollama runs one request at a time
(`OLLAMA_NUM_PARALLEL=1`), so add GPU capacity rather than API replicas to get more throughput.

## Security

- Both pods run as non-root with read-only root filesystems, all capabilities dropped and the `RuntimeDefault` seccomp profile.
- The namespace enforces the Pod Security `restricted` profile.
- A NetworkPolicy allows only the API to reach Ollama.
- The API has no authentication. Put it behind your ingress's authentication, or keep it cluster-internal, before exposing it beyond a test cluster.

## Development

```bash
make venv && make test   # unit tests use a fake Ollama; no model needed
make samples             # regenerate samples/*.pdf and prompts/invoice_summary.pdf
make run                 # hot-reload API against a local `ollama serve`
```
