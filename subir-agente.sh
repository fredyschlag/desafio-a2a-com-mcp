#!/usr/bin/env bash
# Sobe o agente A2A (porta 7300, ou AGENTE_PORT). Rode a partir da raiz do repositorio.
# O servidor MCP precisa estar no ar em MCP_URL (padrao http://localhost:7301/mcp).
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --locked --project agente python agente/agente_a2a.py
