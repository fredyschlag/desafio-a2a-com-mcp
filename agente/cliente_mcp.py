"""Cliente MCP do agente: host MCP por dentro, sem regra de sala.

Fala com o servidor "central-de-salas" por Streamable HTTP, revisao 2026-07-28,
sem sessao de protocolo: cada request leva no `_meta` a versao, o clientInfo e
as capabilities. O objeto `ClienteMCP` fica vivo entre chamadas (conexao HTTP
reaproveitada), o que nao cria estado de protocolo.

Este modulo traduz protocolo e nada mais:

- descobre as tools por `tools/list` antes do primeiro `tools/call`;
- le o resource `politica://uso` e extrai a versao da primeira linha;
- devolve o `input_required` cru como uma `Pausa` (chave, alternativas e o
  `requestState` opaco), sem responder a elicitation sozinho;
- `retomar` repete o `tools/call` com id de JSON-RPC novo (o dispatcher do SDK
  numera cada request), `inputResponses` e o `requestState` ecoado intacto.

O `requestState` nunca e aberto, decodificado, interpretado nem reconstruido:
e so uma `str` guardada e devolvida.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    DiscoverResult,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
    PaginatedRequestParams,
    ServerCapabilities,
    TextResourceContents,
    Tool,
)

__all__ = [
    "PROTOCOLO",
    "CAPABILITIES",
    "ClienteMCP",
    "Concluido",
    "ErroDeExecucao",
    "Pausa",
    "chamadas_ao_callback_de_elicitation",
]

PROTOCOLO = "2026-07-28"
MCP_URL_PADRAO = "http://localhost:7301/mcp"
CLIENT_INFO = Implementation(name="agente-central-de-salas", version="1.0.0")
CAPABILITIES = {"elicitation": {"form": {}}}
URI_POLITICA = "politica://uso"

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Concluido:
    """`tools/call` terminou com `resultType: complete` e sem `isError`."""

    dados: dict[str, Any]
    texto: str


@dataclass(frozen=True)
class Pausa:
    """O servidor respondeu `input_required` com uma elicitation em form mode.

    `request_state` e opaco: o agente so guarda e devolve. Fica fora do repr
    para nao vazar em log ou print por acidente.
    """

    chave: str
    campo: str
    alternativas: list[str]
    mensagem: str
    request_state: str = field(repr=False)


class ErroDeExecucao(Exception):
    """A tool devolveu `isError: true`. `str(erro)` e o texto exato da tool."""


# ---------------------------------------------------------------------------
# Capability de elicitation: so form
# ---------------------------------------------------------------------------

_chamadas_callback = 0


def chamadas_ao_callback_de_elicitation() -> int:
    """Quantas vezes o callback-armadilha de elicitation rodou (deve ser sempre 0)."""
    return _chamadas_callback


async def _elicitation_proibida(context: Any, params: Any) -> Any:
    """Armadilha: o agente nunca responde elicitation sozinho.

    Na revisao 2026-07-28 sobre HTTP nao existe canal de volta, entao o
    servidor nao tem como chamar isto; e `allow_input_required=True` faz o
    `input_required` chegar cru, sem passar por callback. Se algum dia rodar,
    conta e explode.
    """
    global _chamadas_callback
    _chamadas_callback += 1
    print("mcp-client ERRO: callback de elicitation chamado", file=sys.stderr, flush=True)
    raise RuntimeError("o agente nao responde elicitation sozinho: a pergunta tem de voltar ao cliente A2A")


class _SessaoSoForm(ClientSession):
    """`ClientSession` que anuncia exatamente `{"elicitation": {"form": {}}}`.

    Limitacao do SDK 2.3.0 (mcp/client/session.py, `_build_capabilities`): a
    capability de elicitation so e anunciada quando ha `elicitation_callback`,
    e entao sempre como form + url:

        elicitation = (
            types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
            if self._elicitation_callback is not _default_elicitation_callback
            else None
        )

    e o stamp moderno sobrescreve o `_meta` de cada request com esse valor
    (`meta[CLIENT_CAPABILITIES_META_KEY] = capabilities`), entao nem `meta=` por
    chamada consegue corrigir. Nao ha parametro publico para escolher os modes.
    O ajuste minimo e trocar so o anuncio; versao, clientInfo, headers e ids
    continuam saindo do proprio SDK.
    """

    def _build_capabilities(self, version: str) -> ClientCapabilities:
        return ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


# ---------------------------------------------------------------------------
# traceparent
# ---------------------------------------------------------------------------


class _Trace:
    """Mantem o trace-id de uma operacao e gera um span-id novo por request."""

    def __init__(self, traceparent: str | None) -> None:
        m = _TRACEPARENT.match(traceparent.strip().lower()) if traceparent else None
        if m and m.group(2) != "0" * 32 and m.group(3) != "0" * 16:
            self.trace_id, self.flags = m.group(2), m.group(4)
        else:
            self.trace_id, self.flags = secrets.token_hex(16), "01"

    def meta(self) -> dict[str, Any]:
        return {"traceparent": f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"}


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


async def _log_headers(request: httpx2.Request) -> None:
    h = request.headers
    print(
        f"mcp-client-headers {request.method} {request.url.path} "
        f"MCP-Protocol-Version={h.get('MCP-Protocol-Version', '-')} "
        f"Mcp-Method={h.get('Mcp-Method', '-')} Mcp-Name={h.get('Mcp-Name', '-')}",
        file=sys.stderr,
        flush=True,
    )


class ClienteMCP:
    """Cliente MCP de vida longa. Use como `async with ClienteMCP() as mcp: ...`."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("MCP_URL", MCP_URL_PADRAO)
        self._pilha: AsyncExitStack | None = None
        self._sessao: ClientSession | None = None
        self._tools: dict[str, Tool] | None = None

    async def __aenter__(self) -> ClienteMCP:
        pilha = AsyncExitStack()
        try:
            hooks = {"request": [_log_headers]} if os.environ.get("MCP_DEBUG_HEADERS") == "1" else {}
            http = await pilha.enter_async_context(
                httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, read=60.0), event_hooks=hooks)
            )
            leitura, escrita = await pilha.enter_async_context(streamable_http_client(self.url, http_client=http))
            sessao = await pilha.enter_async_context(
                _SessaoSoForm(
                    dispatcher=JSONRPCDispatcher(leitura, escrita),
                    client_info=CLIENT_INFO,
                    elicitation_callback=_elicitation_proibida,
                )
            )
            # Revisao fixada, sem handshake nem server/discover: e o que `Client(mode="2026-07-28")`
            # faz. Daqui em diante o SDK carimba _meta e headers em todo request.
            sessao.adopt(
                DiscoverResult(
                    supported_versions=[PROTOCOLO],
                    capabilities=ServerCapabilities(),
                    result_type="complete",
                    ttl_ms=0,
                    cache_scope="private",
                )
            )
            anunciado = sessao._build_capabilities(PROTOCOLO).model_dump(by_alias=True, mode="json", exclude_none=True)
            if anunciado != CAPABILITIES:
                raise RuntimeError(f"capabilities anunciadas {anunciado} diferem de {CAPABILITIES}")
        except BaseException:
            await pilha.aclose()
            raise
        self._pilha, self._sessao = pilha, sessao
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pilha is not None:
            await self._pilha.aclose()
        self._pilha = self._sessao = None

    @property
    def _s(self) -> ClientSession:
        if self._sessao is None:
            raise RuntimeError("use ClienteMCP dentro de 'async with'")
        return self._sessao

    # -- descoberta e politica ---------------------------------------------

    async def descobrir(self, traceparent: str | None = None) -> dict[str, Tool]:
        """`tools/list` (com paginacao). Atualiza o catalogo usado pelos `tools/call`."""
        trace = _Trace(traceparent)
        tools: dict[str, Tool] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"_meta": trace.meta()}
            if cursor:
                params["cursor"] = cursor
            resultado = await self._s.list_tools(params=PaginatedRequestParams.model_validate(params))
            tools.update({t.name: t for t in resultado.tools})
            cursor = resultado.next_cursor
            if not cursor:
                break
        self._tools = tools
        return tools

    async def versao_da_politica(self, traceparent: str | None = None) -> str:
        """`resources/read` de politica://uso e a versao declarada na primeira linha."""
        resultado = await self._s.read_resource(URI_POLITICA, meta=_Trace(traceparent).meta())
        texto = next((c.text for c in resultado.contents if isinstance(c, TextResourceContents)), "")
        primeira = texto.splitlines()[0] if texto else ""
        m = re.match(r"\s*versao:\s*(\S+)", primeira)
        if not m:
            raise ValueError(f"{URI_POLITICA} nao declara 'versao:' na primeira linha")
        return m.group(1)

    async def _tool(self, nome: str, argumentos: dict[str, Any], trace: _Trace) -> Tool:
        if self._tools is None or nome not in self._tools:
            await self.descobrir(f"00-{trace.trace_id}-{secrets.token_hex(8)}-{trace.flags}")
        assert self._tools is not None
        tool = self._tools.get(nome)
        if tool is None:
            raise LookupError(f"o servidor MCP nao oferece a tool {nome!r} (tools: {sorted(self._tools)})")
        schema = tool.input_schema or {}
        propriedades = set((schema.get("properties") or {}).keys())
        obrigatorios = set(schema.get("required") or [])
        if sobra := set(argumentos) - propriedades:
            raise ValueError(f"{nome}: o inputSchema nao tem os campos {sorted(sobra)}")
        if falta := obrigatorios - set(argumentos):
            raise ValueError(f"{nome}: faltam os campos obrigatorios {sorted(falta)}")
        return tool

    # -- tools/call ----------------------------------------------------------

    async def chamar(self, nome: str, argumentos: dict[str, Any], traceparent: str | None = None) -> Concluido | Pausa:
        """`tools/call` generico, devolvendo o `input_required` cru como `Pausa`."""
        trace = _Trace(traceparent)
        await self._tool(nome, argumentos, trace)
        resultado = await self._s.call_tool(nome, argumentos, meta=trace.meta(), allow_input_required=True)
        return self._interpretar(resultado)

    async def reservar(self, argumentos: dict[str, Any], traceparent: str | None = None) -> Concluido | Pausa:
        """`tools/call` de reservar_sala. Conflito volta como `Pausa`, nunca respondido aqui."""
        return await self.chamar("reservar_sala", argumentos, traceparent)

    async def retomar(
        self,
        argumentos: dict[str, Any],
        chave: str,
        resposta: dict[str, Any] | ElicitResult,
        request_state: str,
        traceparent: str | None = None,
        nome: str = "reservar_sala",
    ) -> Concluido | Pausa:
        """Retry do `tools/call` original: id novo, `inputResponses={chave: resposta}`, estado ecoado.

        `argumentos` sao os do pedido original, sem alteracao. `resposta` e um
        ElicitResult (`{"action": "accept", "content": {...}}`, `{"action": "decline"}`
        ou `{"action": "cancel"}`).
        """
        trace = _Trace(traceparent)
        await self._tool(nome, argumentos, trace)
        elicit = resposta if isinstance(resposta, ElicitResult) else ElicitResult.model_validate(resposta)
        resultado = await self._s.call_tool(
            nome,
            argumentos,
            meta=trace.meta(),
            input_responses={chave: elicit},
            request_state=request_state,
            allow_input_required=True,
        )
        return self._interpretar(resultado)

    @staticmethod
    def _interpretar(resultado: Any) -> Concluido | Pausa:
        if isinstance(resultado, InputRequiredResult):
            return _pausa_de(resultado)
        if not isinstance(resultado, CallToolResult):
            raise MCPError(code=-32603, message=f"resultado inesperado do servidor MCP: {type(resultado).__name__}")
        texto = " ".join(getattr(c, "text", "") for c in resultado.content).strip()
        if resultado.is_error:
            raise ErroDeExecucao(texto)
        return Concluido(dados=dict(resultado.structured_content or {}), texto=texto)


def _pausa_de(resultado: InputRequiredResult) -> Pausa:
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or resultado.request_state is None:
        raise MCPError(code=-32603, message="input_required sem exatamente uma elicitation e um requestState")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, ElicitRequest) or not isinstance(pedido.params, ElicitRequestFormParams):
        raise MCPError(code=-32603, message="input_required com um pedido que nao e elicitation em form mode")
    schema = pedido.params.requested_schema or {}
    propriedades = schema.get("properties") or {}
    obrigatorios = schema.get("required") or []
    campo = obrigatorios[0] if len(obrigatorios) == 1 else next(iter(propriedades), None)
    definicao = propriedades.get(campo) or {}
    if "enum" in definicao:
        alternativas = [str(v) for v in definicao["enum"]]
    elif "const" in definicao:
        alternativas = [str(definicao["const"])]
    else:
        raise MCPError(code=-32603, message="a elicitation nao restringe a escolha por enum ou const")
    return Pausa(
        chave=chave,
        campo=campo,
        alternativas=alternativas,
        mensagem=pedido.params.message,
        request_state=resultado.request_state,
    )
