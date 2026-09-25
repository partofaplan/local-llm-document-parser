import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"
PROMPTS = ROOT / "prompts"


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not (SAMPLES / "sample_invoice.pdf").exists() or not (PROMPTS / "invoice_summary.pdf").exists():
        import runpy

        runpy.run_path(str(ROOT / "scripts" / "make_samples.py"), run_name="__main__")


class FakeOllama:
    """httpx transport imitating the Ollama endpoints the app uses."""

    def __init__(self, responses=None, models=("qwen2.5:3b",)):
        self.responses = list(responses or [])
        self.models = list(models)
        self.chat_calls = []
        self.pulled = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/pull":
            name = json.loads(request.content)["model"]
            self.pulled.append(name)
            self.models.append(name)
            return httpx.Response(200, content=b'{"status":"pulling"}\n{"status":"success"}\n')
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            self.chat_calls.append(body)
            data = self.responses.pop(0) if self.responses else {}
            return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(data)}})
        return httpx.Response(404)

    def transport(self):
        return httpx.MockTransport(self)
