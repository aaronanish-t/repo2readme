FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

USER app
# Grammars are downloaded on first use; fetch the common ones at build time so requests don't wait on them.
RUN python -c "import tree_sitter_language_pack as p; p.download(['python','javascript','typescript','tsx','go','rust','java','kotlin','scala','c','cpp','csharp','ruby','php','swift','bash','lua','elixir','dart','vue','svelte','zig','haskell','ocaml','clojure','r','julia','perl','sql','hcl'])"

ENV PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "uvicorn repo2readme.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
