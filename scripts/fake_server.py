"""Run the hosted demo with the real clone/parse pipeline but a fake model (no API key, no cost).

    python scripts/fake_server.py  ->  http://127.0.0.1:8765
"""

import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fakes import FakeLLM  # noqa: E402

from repo2readme.web.app import create_app  # noqa: E402

app = create_app(llm_factory=FakeLLM)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
