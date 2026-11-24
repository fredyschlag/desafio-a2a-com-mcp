"""Servidor MCP "central-de-salas" (revisao 2026-07-28, Streamable HTTP sem sessao).

Tools: listar_salas, consultar_disponibilidade, reservar_sala (com MRTR).
Resource: politica://uso.

Subir a partir da raiz do repositorio (o segredo vem do ambiente, nunca do codigo):

    set -a; . ./.env; set +a; uv run --project servidor-mcp python servidor-mcp/servidor.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Dados: lidos de <raiz>/dados, sem copia.
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in SALAS}
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
_m = re.match(r"\s*versao:\s*(\S+)", POLITICA_TEXTO)
POLITICA_VERSAO = _m.group(1) if _m else None

FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)

# Reservas em memoria: as do arquivo + as criadas neste processo.
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
_proximo_numero = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0) + 1

# Mensagens exatas do enunciado.
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
CHAVE_ELICITATION = "escolha_de_sala"
MAX_ALTERNATIVAS = 3


# ---------------------------------------------------------------------------
# Modelos de saida (geram os outputSchema de exemplos/wire/01).
# ---------------------------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Regras de dominio compartilhadas por consultar e reservar.
# ---------------------------------------------------------------------------


def _parse_instante(valor: str, campo: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise ToolError(f"Data invalida em {campo}: use ISO 8601 com fuso, ex. 2026-11-03T14:00:00-03:00") from None
    if instante.tzinfo is None:
        raise ToolError(f"Data invalida em {campo}: use ISO 8601 com fuso, ex. 2026-11-03T14:00:00-03:00")
    return instante.astimezone(FUSO_SP)


def validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Ordem das checagens: sala, formato, intervalo, janela, duracao.

    Bate com as verificacoes 9 a 12 do validador: sala inexistente vem antes de
    qualquer olhar nas datas; o intervalo invertido e checado antes da janela e
    da duracao porque "duracao" so tem sentido num intervalo valido.
    """
    if sala not in SALAS_POR_ID:
        raise ToolError(ERRO_SALA.format(sala=sala))
    ini = _parse_instante(inicio, "inicio")
    fi = _parse_instante(fim, "fim")
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    for instante in (ini, fi):
        if not (JANELA_INICIO <= instante.time() <= JANELA_FIM):
            raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def _iso(instante: datetime) -> str:
    return instante.astimezone(FUSO_SP).isoformat()


def conflitos_de(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    resultado = []
    for r in RESERVAS:
        if r["sala"] != sala:
            continue
        r_ini = datetime.fromisoformat(r["inicio"])
        r_fim = datetime.fromisoformat(r["fim"])
        if ini < r_fim and r_ini < fi:
            resultado.append(r)
    return resultado


def alternativas_para(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, ate 3, por (capacidade, id)."""
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s for s in SALAS if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos_de(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, ini: datetime, fi: datetime, responsavel: str) -> ReservaOut:
    global _proximo_numero
    reserva = {
        "id": f"res-{_proximo_numero:04d}",
        "sala": sala,
        "inicio": _iso(ini),
        "fim": _iso(fi),
        "responsavel": responsavel,
    }
    _proximo_numero += 1
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=sala,
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


# ---------------------------------------------------------------------------
# MRTR: pergunta, capability e retry.
# ---------------------------------------------------------------------------


def _exigir_elicitation_form(ctx: Context) -> None:
    """-32021 quando o cliente nao declarou elicitation em form mode neste request.

    Mesma regra do SDK: `elicitation: {}` (forma anterior aos modes) conta como
    form; `elicitation: {url: {}}` sozinho nao conta.
    """
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    dados = MissingRequiredClientCapabilityErrorData(
        required_capabilities=ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    )
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required to choose an alternative room",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def _perguntar(ctx: Context, sala: str, ini: datetime, fi: datetime, responsavel: str) -> InputRequiredResult:
    alternativas = alternativas_para(sala, ini, fi)
    if not alternativas:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_form(ctx)
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            message=MENSAGEM_ELICITATION,
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": alternativas,
                    }
                },
                "required": ["sala"],
            },
        )
    )
    # Plaintext do estado: o RequestStateBoundary do SDK sela (AES-256-GCM + exp)
    # na saida e verifica na volta, antes de a tool enxergar qualquer coisa.
    estado = {
        "sala": sala,
        "inicio": _iso(ini),
        "fim": _iso(fi),
        "responsavel": responsavel,
        "alternativas": alternativas,
    }
    return InputRequiredResult(
        input_requests={CHAVE_ELICITATION: pergunta},
        request_state=json.dumps(estado, separators=(",", ":")),
    )


def _retomar(ctx: Context, estado_bruto: str) -> ReservaOut | InputRequiredResult:
    """Retry: tudo vem do estado selado; os `arguments` reenviados sao ignorados."""
    try:
        estado = json.loads(estado_bruto)
        sala = estado["sala"]
        ini = datetime.fromisoformat(estado["inicio"])
        fi = datetime.fromisoformat(estado["fim"])
        responsavel = estado["responsavel"]
        alternativas = list(estado["alternativas"])
    except (ValueError, KeyError, TypeError):
        raise MCPError(code=INVALID_PARAMS, message="Invalid or expired requestState") from None

    resposta = (ctx.input_responses or {}).get(CHAVE_ELICITATION)
    if not isinstance(resposta, ElicitResult):
        raise MCPError(
            code=INVALID_PARAMS,
            message=f"inputResponses precisa trazer a resposta de elicitation na chave {CHAVE_ELICITATION!r}",
        )
    if resposta.action in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado")

    escolhida = (resposta.content or {}).get("sala")
    if not isinstance(escolhida, str) or escolhida not in alternativas:
        raise MCPError(
            code=INVALID_PARAMS,
            message="inputResponses: a sala escolhida nao esta entre as alternativas oferecidas",
            data={"alternativas": alternativas},
        )
    if conflitos_de(escolhida, ini, fi):
        # A escolhida foi ocupada entre a pergunta e o retry: pergunta de novo com o mundo atual.
        return _perguntar(ctx, sala, ini, fi, responsavel)
    return criar_reserva(escolhida, ini, fi, responsavel)


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------


def _segredo_do_ambiente() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET")
    if not segredo:
        sys.exit(
            "erro: REQUEST_STATE_SECRET nao definida. Gere uma com\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"\n'
            "e exporte antes de subir o servidor (ex.: set -a; . ./.env; set +a)."
        )
    if len(segredo.encode()) < 32:
        sys.exit(
            "erro: REQUEST_STATE_SECRET tem menos de 32 bytes. Gere uma com\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


def criar_servidor(segredo: str) -> MCPServer:
    mcp = MCPServer(
        name="central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[segredo], ttl=600),
    )

    @mcp.tool(description="Lista todas as salas com capacidade e recursos.")
    def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])

    @mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        ini, fi = validar_pedido(sala, inicio, fim)
        conflitos = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos_de(sala, ini, fi)]
        return Disponibilidade(sala=sala, livre=not conflitos, conflitos=conflitos)

    @mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
    def reservar_sala(
        sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
    ) -> ReservaOut | InputRequiredResult:
        if ctx.request_state is not None:
            return _retomar(ctx, ctx.request_state)
        ini, fi = validar_pedido(sala, inicio, fim)
        if conflitos_de(sala, ini, fi):
            return _perguntar(ctx, sala, ini, fi, responsavel)
        return criar_reserva(sala, ini, fi, responsavel)

    @mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
                  description="Politica de uso das salas. A primeira linha declara a versao.")
    def politica_de_uso() -> str:
        return POLITICA_TEXTO

    return mcp


# ---------------------------------------------------------------------------
# Log de requests no stderr (inclusive os rejeitados pelo transporte).
# ---------------------------------------------------------------------------

_TOKEN_SIMPLES = re.compile(r"^[\w.:/@+-]+$")


def _campo(valor: Any) -> str:
    if valor is None:
        return "-"
    texto = valor if isinstance(valor, str) else json.dumps(valor)
    return texto if _TOKEN_SIMPLES.match(texto) else json.dumps(texto)


def _resumo(mensagem: Any) -> tuple[Any, Any, Any, Any]:
    if not isinstance(mensagem, dict):
        return None, None, None, None
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    nome = params.get("name") or params.get("uri")
    return mensagem.get("method"), mensagem.get("id"), nome, meta.get("traceparent")


class LogDeRequests:
    """ASGI por fora do app do SDK: uma linha `mcp-request ...` por POST em /mcp.

    Fica fora do transporte para registrar tambem o que ele rejeita antes do
    handler (sem _meta, header divergente, JSON invalido).
    """

    def __init__(self, app: Any, caminho: str = "/mcp") -> None:
        self.app = app
        self.caminho = caminho

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != self.caminho:
            await self.app(scope, receive, send)
            return

        corpo = b""
        mensagens = []
        while True:
            m = await receive()
            mensagens.append(m)
            if m["type"] == "http.request":
                corpo += m.get("body", b"")
                if not m.get("more_body"):
                    break
            else:
                break

        fila = list(mensagens)

        async def repetir() -> Any:
            if fila:
                return fila.pop(0)
            return await receive()

        status: dict[str, Any] = {"http": None}

        async def enviar(msg: Any) -> None:
            if msg["type"] == "http.response.start":
                status["http"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, repetir, enviar)
        finally:
            try:
                bruto = json.loads(corpo) if corpo else None
            except ValueError:
                bruto = None
            itens = bruto if isinstance(bruto, list) else [bruto]
            for item in itens:
                metodo, rid, nome, traceparent = _resumo(item)
                print(
                    f"mcp-request http_method={scope.get('method')} method={_campo(metodo)} id={_campo(rid)} "
                    f"name={_campo(nome)} traceparent={_campo(traceparent)} status={_campo(status['http'])}",
                    file=sys.stderr,
                    flush=True,
                )


def main() -> None:
    segredo = _segredo_do_ambiente()
    porta = int(os.environ.get("MCP_PORT", "7301"))
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp = criar_servidor(segredo)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=host,
    )
    print(
        f"central-de-salas 1.0.0 em http://{host}:{porta}/mcp (politica {POLITICA_VERSAO}, "
        f"{len(SALAS)} salas, {len(RESERVAS)} reservas iniciais)",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(LogDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
