#!/usr/bin/env bash
# Sobe o servidor MCP (porta 7301, ou MCP_PORT). Rode a partir da raiz do repositorio.
# O segredo vem do ambiente: exporte REQUEST_STATE_SECRET antes, ou tenha um .env na raiz.
set -euo pipefail
cd "$(dirname "$0")"
if [ -z "${REQUEST_STATE_SECRET:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
exec uv run --project servidor-mcp python servidor-mcp/servidor.py
