"""Demonstracao do agente como cliente MCP puro (sem A2A), contra o servidor rodando.

    uv run --locked --project agente python agente/demo_mcp.py sala-fusca
    uv run --locked --project agente python agente/demo_mcp.py recusar

1. descobre as tools (tools/list) e le a versao da politica (resources/read);
2. pede sala-garagem das 14h as 15h de 03/11/2026 para Marty e mostra a pausa;
3. retoma com a escolha do argumento (uma sala, ou "recusar");
4. pede uma sala inexistente e mostra o erro de execucao da tool.

Todos os requests MCP levam o mesmo trace-id (--traceparent, ou um gerado agora).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

from cliente_mcp import ClienteMCP, Concluido, ErroDeExecucao, Pausa, chamadas_ao_callback_de_elicitation

PEDIDO = {
    "sala": "sala-garagem",
    "inicio": "2026-11-03T14:00:00-03:00",
    "fim": "2026-11-03T15:00:00-03:00",
    "responsavel": "Marty",
}
PEDIDO_INEXISTENTE = {**PEDIDO, "sala": "sala-inexistente"}


def mostrar(rotulo: str, resultado: Concluido | Pausa) -> None:
    if isinstance(resultado, Pausa):
        print(f"{rotulo}: PAUSA (input_required)")
        print(f"  mensagem:     {resultado.mensagem}")
        print(f"  chave:        {resultado.chave}")
        print(f"  campo:        {resultado.campo}")
        print(f"  alternativas: {', '.join(resultado.alternativas)}")
        print(f"  requestState: <opaco, {len(resultado.request_state)} caracteres, guardado sem abrir>")
    else:
        print(f"{rotulo}: CONCLUIDO")
        print("  " + json.dumps(resultado.dados, ensure_ascii=False))


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("escolha", help='id de uma das salas alternativas, ou "recusar"')
    p.add_argument("--traceparent", default=None)
    args = p.parse_args()
    traceparent = args.traceparent or f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    print(f"trace-id: {traceparent.split('-')[1]}")

    async with ClienteMCP() as mcp:
        tools = await mcp.descobrir(traceparent)
        print(f"tools descobertas: {', '.join(sorted(tools))}")
        print(f"politica: {await mcp.versao_da_politica(traceparent)}")
        print()

        print(f"pedido: {json.dumps(PEDIDO, ensure_ascii=False)}")
        resultado = await mcp.reservar(PEDIDO, traceparent)
        mostrar("reservar", resultado)

        if isinstance(resultado, Pausa):
            if args.escolha == "recusar":
                resposta = {"action": "decline"}
            else:
                resposta = {"action": "accept", "content": {resultado.campo: args.escolha}}
            print(f"\nescolha: {args.escolha} -> inputResponses[{resultado.chave!r}] = {json.dumps(resposta)}")
            try:
                retomado = await mcp.retomar(PEDIDO, resultado.chave, resposta, resultado.request_state, traceparent)
                mostrar("retomar", retomado)
            except ErroDeExecucao as e:
                print(f"retomar: ERRO DE EXECUCAO: {e}")

        print(f"\npedido: {json.dumps(PEDIDO_INEXISTENTE, ensure_ascii=False)}")
        try:
            mostrar("reservar", await mcp.reservar(PEDIDO_INEXISTENTE, traceparent))
        except ErroDeExecucao as e:
            print(f"reservar: ERRO DE EXECUCAO: {e}")

    print(f"\ncallback de elicitation executado: {chamadas_ao_callback_de_elicitation()} vez(es)")
    return 0 if chamadas_ao_callback_de_elicitation() == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
