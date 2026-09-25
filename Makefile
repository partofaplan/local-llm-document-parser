IMAGE      ?= docparser-api
TAG        ?= dev
REGISTRY   ?=
PLATFORMS  ?= linux/amd64,linux/arm64
CLUSTER    ?= picard
CONTEXT    ?= $(CLUSTER)
NS         ?= docparser
PY         ?= .venv/bin/python

.PHONY: help venv test samples run build buildx picard-import picard-deploy picard-status picard-logs picard-forward picard-smoke picard-delete compose-up compose-down

help: ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

venv: ## Create .venv with dev dependencies
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test: ## Run unit tests
	$(PY) -m pytest -q

samples: ## Regenerate sample PDFs and the invoice form template
	$(PY) scripts/make_samples.py

run: ## Run the API locally against Ollama on localhost:11434
	.venv/bin/uvicorn app.main:app --reload --port 8080

build: ## Build the API image for the local architecture
	docker build -t $(IMAGE):$(TAG) .

buildx: ## Build and push a multi-arch image (REGISTRY=registry.example.com)
	docker buildx build --platform $(PLATFORMS) -t $(REGISTRY)/$(IMAGE):$(TAG) --push .

picard-import: build ## Build and import the image into the k3d cluster
	k3d image import $(IMAGE):$(TAG) -c $(CLUSTER)

picard-deploy: picard-import ## Deploy to the local picard cluster
	kubectl --context $(CONTEXT) apply -k deploy/k8s/overlays/picard
	kubectl --context $(CONTEXT) -n $(NS) rollout restart deploy/docparser-api
	@echo "Waiting for the model to download and the API to become ready (first run takes a few minutes)..."
	kubectl --context $(CONTEXT) -n $(NS) rollout status deploy/ollama --timeout=10m
	kubectl --context $(CONTEXT) -n $(NS) rollout status deploy/docparser-api --timeout=20m
	@echo "Open http://docparser.localhost:8080"

picard-status: ## Show pods and readiness
	kubectl --context $(CONTEXT) -n $(NS) get pods,svc,ingress,pvc
	-curl -s http://docparser.localhost:8080/readyz; echo

picard-logs: ## Tail API logs
	kubectl --context $(CONTEXT) -n $(NS) logs -f deploy/docparser-api

picard-forward: ## Port-forward the API to localhost:8081 (bypasses ingress)
	kubectl --context $(CONTEXT) -n $(NS) port-forward svc/docparser-api 8081:80

picard-smoke: ## Extract + fill the sample invoice through the ingress
	curl -sf --retry 5 --retry-all-errors -F file=@samples/sample_invoice.pdf -F prompt=invoice http://docparser.localhost:8080/api/extract | $(PY) -m json.tool
	curl -sf --retry 5 --retry-all-errors -F file=@samples/sample_invoice.pdf -F prompt=invoice -o out/filled-invoice.pdf --create-dirs http://docparser.localhost:8080/api/fill && echo "wrote out/filled-invoice.pdf"

picard-delete: ## Remove the deployment (keeps nothing, including the model volume)
	kubectl --context $(CONTEXT) delete -k deploy/k8s/overlays/picard

compose-up: ## Run with Docker Compose on any Linux host
	docker compose up -d --build

compose-down:
	docker compose down
