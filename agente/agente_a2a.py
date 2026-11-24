"""Agente "Central de Salas": servidor A2A v1.0 por fora, host MCP por dentro.

    uv run --locked --project agente python agente/agente_a2a.py

A ponte acontece em `ExecutorDaPonte`:

- `_tratar_resultado` recebe a `Pausa` (o `input_required` cru do servidor MCP),
  guarda chave, campo, alternativas e `requestState` em `self._pausas[task_id]`
  e poe a Task em TASK_STATE_INPUT_REQUIRED com a linha `alternativas: ...`;
- `_continuar` recebe o `escolha=<valor>`, e `ClienteMCP.retomar` repete o
  `tools/call` com id de JSON-RPC novo, `inputResponses` e o `requestState`
  ecoado sem modificacao.

O agente nao decide nada de sala: so traduz protocolo.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import uvicorn
from a2a.helpers.proto_helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Task,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, VERSION_HEADER
from a2a.utils.errors import UnsupportedOperationError
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from cliente_mcp import ClienteMCP, Concluido, ErroDeExecucao, Pausa

ROTA_A2A = "/a2a"
URL_PUBLICA_PADRAO = "http://localhost:7300/a2a"
EXEMPLO_PEDIDO = (
    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
)
FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

_PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
_ESCOLHA = re.compile(r"^escolha=(\S+)$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
RECUSAR = "recusar"


def log(linha: str) -> None:
    print(linha, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Agent Card (forma de exemplos/wire/07)
# ---------------------------------------------------------------------------


def criar_card(url_publica: str) -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=url_publica, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[EXEMPLO_PEDIDO],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Versao A2A: sem header, este endpoint fala 1.0
# ---------------------------------------------------------------------------


class ContextoVersao10PorPadrao(DefaultServerCallContextBuilder):
    """Sem `A2A-Version`, assume 1.0 (a unica versao que o card declara).

    O a2a-sdk 1.3.1 trata header ausente como 0.3 (`a2a/utils/version_validator.py`:
    `if not actual_version: return constants.PROTOCOL_VERSION_0_3`) e recusa
    `SendMessage`/`GetTask` com "Version mismatch". O cliente do contrato
    (validador e exemplos/wire/08 a 10) nao envia o header. O ajuste usa o
    ponto de extensao publico `context_builder` de `create_jsonrpc_routes`;
    um cliente que declare explicitamente outra versao continua recusado.
    """

    def build(self, request: Any) -> ServerCallContext:
        contexto = super().build(request)
        headers = contexto.state.setdefault("headers", {})
        if not (headers.get(VERSION_HEADER) or headers.get(VERSION_HEADER.lower())):
            headers[VERSION_HEADER.lower()] = PROTOCOL_VERSION_1_0
        return contexto


# ---------------------------------------------------------------------------
# TaskStore: a mensagem de status tambem entra no history
# ---------------------------------------------------------------------------


class TaskStoreComHistorico(InMemoryTaskStore):
    """InMemoryTaskStore que mantem a mensagem de status corrente no history.

    O SDK so move `status.message` para o history quando chega o proximo
    status; numa Task terminal a ultima mensagem (por exemplo o erro exato da
    tool) nunca entraria. Aqui, ao salvar, a mensagem de status entra no
    history se ainda nao estiver, e mensagens repetidas (mesmo messageId) sao
    descartadas: e a forma de exemplos/wire/08 e 10.
    """

    async def save(self, task: Task, context: ServerCallContext) -> None:
        vistos: set[str] = set()
        historico = []
        candidatas = list(task.history)
        if task.status.HasField("message"):
            candidatas.append(task.status.message)
        for m in candidatas:
            if m.message_id and m.message_id in vistos:
                continue
            vistos.add(m.message_id)
            historico.append(m)
        if len(historico) != len(task.history):
            del task.history[:]
            task.history.extend(historico)
        await super().save(task, context)


# ---------------------------------------------------------------------------
# Executor: a ponte
# ---------------------------------------------------------------------------


@dataclass
class PausaGuardada:
    """Tudo que o agente precisa para retomar uma Task. Fica fora da Task."""

    argumentos: dict[str, str]
    chave: str
    campo: str
    alternativas: list[str]
    politica: str
    traceparent: str
    request_state: str = field(repr=False)

    def linha(self) -> str:
        return "alternativas: " + ", ".join(self.alternativas)


class ExecutorDaPonte(AgentExecutor):
    def __init__(self) -> None:
        self.mcp: ClienteMCP | None = None
        self._pausas: dict[str, PausaGuardada] = {}

    # -- utilitarios -------------------------------------------------------

    @staticmethod
    def _traceparent_do_header(context: RequestContext) -> str:
        headers = context.call_context.state.get("headers") or {}
        valor = str(headers.get("traceparent", "")).strip().lower()
        m = _TRACEPARENT.match(valor)
        trace_id = m.group(1) if m and m.group(1) != "0" * 32 else secrets.token_hex(16)
        flags = m.group(3) if m else "01"
        return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"

    @staticmethod
    async def _status(updater: TaskUpdater, estado: int, texto: str | None = None) -> None:
        mensagem = updater.new_agent_message([new_text_part(texto)]) if texto is not None else None
        await updater.update_status(estado, message=mensagem)
        log(f"a2a-task task={updater.task_id} state={TaskState.Name(estado)}")

    def _cliente(self) -> ClienteMCP:
        if self.mcp is None:
            raise RuntimeError("ClienteMCP nao iniciado")
        return self.mcp

    # -- AgentExecutor -----------------------------------------------------

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = str(context.task_id), str(context.context_id)
        updater = TaskUpdater(event_queue, task_id, context_id)
        texto = context.get_user_input().strip()
        try:
            if context.current_task is None:
                await self._nova(context, updater, event_queue, texto)
            else:
                await self._continuar(updater, texto)
        except ErroDeExecucao as e:
            await self._encerrar(updater, TaskState.TASK_STATE_FAILED, str(e))
        except MCPError as e:
            await self._encerrar(
                updater, TaskState.TASK_STATE_FAILED, f"Erro do servidor MCP ({e.error.code}): {e.error.message}"
            )
        except ConnectionError as e:
            await self._encerrar(updater, TaskState.TASK_STATE_FAILED, f"Falha ao falar com o servidor MCP: {e}")
        except (OSError, TimeoutError) as e:
            await self._encerrar(updater, TaskState.TASK_STATE_FAILED, f"Servidor MCP indisponivel: {type(e).__name__}")
        except Exception as e:  # nunca deixar a Task presa em WORKING
            log(f"a2a-erro task={task_id} tipo={type(e).__name__}")
            await self._encerrar(updater, TaskState.TASK_STATE_FAILED, f"Falha interna do agente: {type(e).__name__}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(message="CancelTask nao e suportado: responda escolha=recusar")

    # -- fluxo ---------------------------------------------------------------

    async def _nova(self, context: RequestContext, updater: TaskUpdater, fila: EventQueue, texto: str) -> None:
        historico = [context.message] if context.message is not None else []
        await fila.enqueue_event(
            new_task(updater.task_id, updater.context_id, TaskState.TASK_STATE_SUBMITTED, history=historico)
        )
        log(f"a2a-task task={updater.task_id} state=TASK_STATE_SUBMITTED")
        await self._status(updater, TaskState.TASK_STATE_WORKING)

        m = _PEDIDO.match(texto)
        if not m:
            await self._encerrar(
                updater, TaskState.TASK_STATE_FAILED, f"Pedido fora do formato. Use: {FORMATO_PEDIDO}"
            )
            return
        argumentos = dict(zip(("sala", "inicio", "fim", "responsavel"), m.groups()))
        traceparent = self._traceparent_do_header(context)
        mcp = self._cliente()
        politica = await mcp.versao_da_politica(traceparent)
        resultado = await mcp.reservar(argumentos, traceparent)
        await self._tratar_resultado(updater, resultado, argumentos, politica, traceparent)

    async def _continuar(self, updater: TaskUpdater, texto: str) -> None:
        pausa = self._pausas.get(updater.task_id)
        if pausa is None:
            # So acontece se a Task nao estiver pausada por este processo.
            await self._encerrar(
                updater, TaskState.TASK_STATE_FAILED, "Nao ha pergunta pendente para esta Task neste agente."
            )
            return
        m = _ESCOLHA.match(texto)
        escolha = m.group(1) if m else None
        if escolha is None or (escolha != RECUSAR and escolha not in pausa.alternativas):
            # Traducao de protocolo: a resposta tem de caber no enum que o servidor mandou.
            await self._status(updater, TaskState.TASK_STATE_INPUT_REQUIRED, pausa.linha())
            return

        if escolha == RECUSAR:
            resposta: dict[str, Any] = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {pausa.campo: escolha}}
        await self._status(updater, TaskState.TASK_STATE_WORKING)
        resultado = await self._cliente().retomar(
            pausa.argumentos, pausa.chave, resposta, pausa.request_state, pausa.traceparent
        )
        await self._tratar_resultado(updater, resultado, pausa.argumentos, pausa.politica, pausa.traceparent)

    async def _tratar_resultado(
        self,
        updater: TaskUpdater,
        resultado: Concluido | Pausa,
        argumentos: dict[str, str],
        politica: str,
        traceparent: str,
    ) -> None:
        if isinstance(resultado, Pausa):
            # A PONTE, ida: input_required do MCP -> TASK_STATE_INPUT_REQUIRED, requestState guardado fora da Task.
            guardada = PausaGuardada(
                argumentos=argumentos,
                chave=resultado.chave,
                campo=resultado.campo,
                alternativas=list(resultado.alternativas),
                politica=politica,
                traceparent=traceparent,
                request_state=resultado.request_state,
            )
            self._pausas[updater.task_id] = guardada
            await self._status(updater, TaskState.TASK_STATE_INPUT_REQUIRED, guardada.linha())
            return

        dados = resultado.dados
        if dados.get("reservado") is False:
            await self._encerrar(updater, TaskState.TASK_STATE_CANCELED, "Reserva nao realizada: alternativas recusadas.")
            return
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        await updater.add_artifact(
            [new_text_part(json.dumps(reserva, ensure_ascii=False, separators=(",", ":")))], name="reserva"
        )
        await self._encerrar(
            updater, TaskState.TASK_STATE_COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."
        )

    async def _encerrar(self, updater: TaskUpdater, estado: int, texto: str) -> None:
        self._pausas.pop(updater.task_id, None)
        await self._status(updater, estado, texto)


# ---------------------------------------------------------------------------
# Log de metodos A2A (sem corpo, sem requestState)
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"^[\w.:/@+-]+$")


def _campo(valor: Any) -> str:
    if valor is None or valor == "":
        return "-"
    texto = valor if isinstance(valor, str) else json.dumps(valor)
    return texto if _TOKEN.match(texto) else json.dumps(texto)


class LogDeMetodos:
    """Uma linha `a2a-request ...` por request JSON-RPC em /a2a: metodo, id, task e traceparent."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != ROTA_A2A:
            await self.app(scope, receive, send)
            return
        corpo, mensagens = b"", []
        while True:
            m = await receive()
            mensagens.append(m)
            if m["type"] != "http.request":
                break
            corpo += m.get("body", b"")
            if not m.get("more_body"):
                break
        fila = list(mensagens)

        async def repetir() -> Any:
            return fila.pop(0) if fila else await receive()

        status: dict[str, Any] = {}

        async def enviar(msg: Any) -> None:
            if msg["type"] == "http.response.start":
                status["http"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, repetir, enviar)
        finally:
            try:
                req = json.loads(corpo) if corpo else {}
            except ValueError:
                req = {}
            req = req if isinstance(req, dict) else {}
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            msg = params.get("message") if isinstance(params.get("message"), dict) else {}
            task = msg.get("taskId") or params.get("id")
            headers = dict((k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers", []))
            log(
                f"a2a-request method={_campo(req.get('method'))} id={_campo(req.get('id'))} task={_campo(task)} "
                f"traceparent={_campo(headers.get('traceparent'))} status={_campo(status.get('http'))}"
            )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def criar_app(url_publica: str, mcp_url: str | None = None) -> Any:
    card = criar_card(url_publica)
    executor = ExecutorDaPonte()
    handler = DefaultRequestHandler(agent_executor=executor, task_store=TaskStoreComHistorico(), agent_card=card)

    @asynccontextmanager
    async def ciclo_de_vida(app: Starlette) -> AsyncIterator[None]:
        # Um unico ClienteMCP vivo durante todo o processo.
        async with ClienteMCP(mcp_url) as mcp:
            executor.mcp = mcp
            log(f"agente: cliente MCP pronto para {mcp.url}")
            try:
                yield
            finally:
                executor.mcp = None
                await handler.aclose()

    rotas = create_agent_card_routes(card) + create_jsonrpc_routes(
        handler, ROTA_A2A, context_builder=ContextoVersao10PorPadrao()
    )
    return LogDeMetodos(Starlette(routes=rotas, lifespan=ciclo_de_vida))


def main() -> None:
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    url_publica = os.environ.get("AGENTE_URL_PUBLICA", URL_PUBLICA_PADRAO)
    log(f"agente Central de Salas em http://{host}:{porta}{ROTA_A2A} (card publica {url_publica})")
    uvicorn.run(criar_app(url_publica), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
