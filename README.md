# Local LLM Document Parser

A self-contained service that helps you fill in PDF forms by chatting with a local LLM. Upload a fillable
PDF and the assistant asks guided questions about each field, then produces the filled PDF. It remembers
contract details, such as the contract number and renewal date, so the next form for the same contract
is mostly pre-filled.

The same service also extracts structured data from PDFs using YAML prompt files. Nothing leaves your
infrastructure: the model runs in [Ollama](https://ollama.com) next to the API.

```
            ┌──────────────── namespace: docparser ────────────────┐
 PDF  ──▶   │  docparser-api (FastAPI)            ollama            │
 upload     │   ├─ pdfplumber text + tables  ──▶  qwen2.5:3b (CPU)  │
            │   ├─ tesseract OCR (scans)         or any model /GPU  │
            │   ├─ prompt files (ConfigMap)      PVC: model cache   │
            │   ├─ pypdf form filling                               │
            │   └─ guided chat + SQLite contract memory (PVC)       │
            └───────────────────────────────────────────────────────┘
```

**Extraction pipeline:** parse the PDF (OCR any page with no text layer) → pick a prompt file → the LLM returns
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

## Form-filling chat

Open the UI and upload any fillable (AcroForm) PDF. [samples/contract_renewal_form.pdf](samples/contract_renewal_form.pdf)
is a good first try.

1. **Reading the form.** The assistant lists the form's fields. It labels each one from its tooltip or from
   the text printed beside it. The LLM then writes a friendly question per field and decides whether the
   value is a lasting fact about the contract (parties, dates, contract number) or specific to this
   submission (today's date, signatures, comments).
2. **Which contract?** You give the contract number, or **none**. For a contract it already knows, every
   field whose fact is remembered is filled in straight away, and it reminds you when the contract renews.
3. **Guided questions.** Answer in your own words. You can also answer several fields at once ("the
   contractor is Blue Harbor and the client is Northwind"), or ask what a field means. Clear-cut replies,
   such as yes/no, a date, a number, an option or a short value, are accepted without an LLM call, which
   keeps things quick on CPU. You can also type **skip**, **back**, **review**, **fill** or **help**.
4. **Review and fill.** Correct anything in plain language, then download the PDF. Dates are written in
   `DOCPARSER_FORM_DATE_FORMAT`.

Attach the contract itself (📎) at any point. It is read with the `contract` prompt, its details are saved
to that contract, and matching fields are pre-filled.

Contract memory is keyed by contract number, ignoring case and punctuation, so `nw-2026-014` and
`NW 2026 014` are the same contract. Answers to lasting fields are saved as soon as you give them. Each
fact is stored under a canonical name such as `renewal_date` or `contract_number`, so a different form
asking for "Renewal Date" or "Contract No." is pre-filled too. You can review and delete remembered
contracts and facts in the sidebar.

## API

| Endpoint | Description |
| --- | --- |
| `GET /` | Form-filling chat |
| `POST /api/chat` | `template` (fillable PDF) → new chat session: fields, messages, stage |
| `POST /api/chat/{id}/message` | JSON `{"text": "..."}` → updated session |
| `POST /api/chat/{id}/document` | `file` → read a contract PDF, remember its details, pre-fill fields |
| `GET /api/chat/{id}`, `GET /api/chat/{id}/pdf` | Session state; the filled PDF with the current answers |
| `GET /api/contracts[/{number}]` | Remembered contracts and their facts |
| `PUT /api/contracts/{number}` | JSON `{"facts": {...}}` → create the contract or merge facts into it |
| `DELETE /api/contracts/{number}[/facts/{fact}]` | Forget a contract, or one of its facts |
| `GET /tools` | Test UI: upload a PDF, pick a prompt, extract or fill |
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
| `DOCPARSER_DATA_DIR` | `./data` (`/app/data` in the image) | SQLite database with contract memory and chat sessions; mount a volume here |
| `DOCPARSER_FORM_DATE_FORMAT` | `%Y-%m-%d` | How the chat writes dates into forms, e.g. `%m/%d/%Y` |
| `DOCPARSER_SESSION_TTL_HOURS` | `72` | Unfinished chat sessions older than this are removed on startup |
| `DOCPARSER_CONTRACT_PROMPT` | `contract` | Prompt file used to read contracts attached in the chat |

## Sizing

On CPU, `qwen2.5:3b` takes 10–25 s for a one-page document on picard and needs about 3 GB RAM. The
picard overlay limits Ollama to 4.5 GiB because the Docker Desktop VM has about 7.6 GiB for all nodes.
For larger models, raise Docker Desktop's memory or use a GPU node. Ollama runs one request at a time
(`OLLAMA_NUM_PARALLEL=1`), so add GPU capacity rather than API replicas to get more throughput.

## Security

- Both pods run as non-root with read-only root filesystems, all capabilities dropped and the `RuntimeDefault` seccomp profile.
- The namespace enforces the Pod Security `restricted` profile.
- A NetworkPolicy allows only the API to reach Ollama.
- The API has no authentication, and contract memory is shared by everyone who can reach it. Put it behind your ingress's authentication, or keep it cluster-internal, before exposing it beyond a test cluster.
- Contract memory and uploaded forms are stored unencrypted in SQLite on the `docparser-data` PVC. Because the database file needs a single writer, the API runs as one replica with the `Recreate` strategy.

## Development

```bash
make venv && make test   # unit tests use a fake Ollama; no model needed
make samples             # regenerate samples/*.pdf (including the contract renewal form) and prompts/invoice_summary.pdf
make run                 # hot-reload API against a local `ollama serve`
```
