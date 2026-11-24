# A Ponte: um agente A2A com MCP por dentro

Dois processos Python:

- **Servidor MCP `central-de-salas`** (`servidor-mcp/servidor.py`): Streamable HTTP em `/mcp`, revisão `2026-07-28`, sem sessão, com MRTR na reserva.
- **Agente A2A** (`agente/agente_a2a.py`): JSON-RPC em `/a2a`, A2A v1.0. Por dentro, é um cliente MCP (`agente/cliente_mcp.py`) que fala com o servidor acima.

Não há LLM, banco nem ORM. As reservas ficam em memória.

## Como rodar

### Pré-requisitos

- Python 3.10 ou superior. O validador usa o `python3` do sistema.
- [uv](https://docs.astral.sh/uv/). Cada processo é um projeto uv próprio (`servidor-mcp/` e `agente/`), com versões travadas no `pyproject.toml` e no `uv.lock`. Os scripts de subida usam `uv run --locked`, então o `uv` cria cada `.venv` na primeira execução sem alterar o lock.

Todos os comandos abaixo rodam a partir da raiz do clone.

### 1. Segredo do `requestState`

O servidor MCP sela o `requestState` com uma chave derivada de `REQUEST_STATE_SECRET`. Ele se recusa a subir se a variável faltar ou tiver menos de 32 bytes. O comando abaixo gera 64 caracteres hexadecimais direto num `.env` com permissão 600, sem mostrar o valor na tela:

```bash
umask 077; python3 -c 'import secrets; print("REQUEST_STATE_SECRET=" + secrets.token_hex(32))' > .env; chmod 600 .env
```

O `.env` está no `.gitignore` e nunca vai para o repositório. Gere o segredo uma vez só: com um segredo novo, os `requestState` emitidos antes deixam de valer.

Também dá para exportar direto, sem arquivo. Nesse caso, o servidor tem de subir no mesmo shell:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

### 2. Subir os dois processos

Terminal 1, servidor MCP. O script carrega o `.env` se `REQUEST_STATE_SECRET` não estiver no ambiente. O log de cada request sai no stderr deste terminal:

```bash
./subir-servidor-mcp.sh
```

Terminal 2, agente A2A:

```bash
./subir-agente.sh
```

| Variável | Processo | Padrão | Para quê |
|---|---|---|---|
| `MCP_PORT` | servidor MCP | `7301` | porta do endpoint `/mcp` |
| `MCP_HOST` | servidor MCP | `127.0.0.1` | interface de escuta |
| `MCP_URL` | agente | `http://localhost:7301/mcp` | onde o agente encontra o servidor MCP |
| `AGENTE_PORT` | agente | `7300` | porta do `/a2a` e do card |
| `AGENTE_HOST` | agente | `127.0.0.1` | interface de escuta |
| `AGENTE_URL_PUBLICA` | agente | `http://localhost:7300/a2a` | URL anunciada no Agent Card |
| `MCP_DEBUG_HEADERS` | agente | vazio | com `1`, imprime no stderr os headers MCP de cada request |

Cada processo escreve uma linha por request no stderr:

- servidor MCP: `mcp-request ... method=... id=... pv=... caps=... traceparent=...`
- agente: `a2a-request ...` e `a2a-task task=... state=...`

### 3. Validador

> **Reinicie os dois processos antes de cada execução do validador e antes dos passos manuais 7 a 10.** As reservas vivem em memória, e o validador ocupa a `sala-garagem` (e as alternativas) das 14h às 15h de 03/11/2026. Se o processo não for reiniciado, a segunda rodada encontra esses horários tomados. Para reiniciar, faça Ctrl+C nos dois terminais e rode os scripts de novo.

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

A primeira linha da saída traz o trace-id usado. Ele aparece no `traceparent` das linhas do stderr do servidor MCP.

### 4. Roteiro manual com curl

Use um terceiro terminal, com os dois processos recém-iniciados.

**Agent Card** (passo 3 do avaliador):

```bash
curl -s http://localhost:7300/.well-known/agent-card.json | python3 -m json.tool
```

**SendMessage com sala ocupada** (passo 7). O corpo é o de `exemplos/wire/08-a2a-send-message.json`:

```bash
python3 -c 'import json; print(json.dumps(json.load(open("exemplos/wire/08-a2a-send-message.json"))["request"]["body"]))' > /tmp/ponte-08.json
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d @/tmp/ponte-08.json | tee /tmp/ponte-resp.json | python3 -m json.tool
TASK=$(python3 -c 'import json; print(json.load(open("/tmp/ponte-resp.json"))["result"]["task"]["id"])'); echo "$TASK"
```

Esperado: `TASK_STATE_INPUT_REQUIRED` e a mensagem `alternativas: sala-fusca, sala-mirante`.

**Continuação** (passo 8):

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"msg-escolha-1","role":"ROLE_USER","taskId":"'"$TASK"'","parts":[{"text":"escolha=sala-mirante"}]}}}' | python3 -m json.tool
```

**GetTask**:

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"'"$TASK"'"}}' | python3 -m json.tool
```

Esperado: `TASK_STATE_COMPLETED` e o artifact `reserva` com `"sala":"sala-mirante"` e `"politica":"2026-11-01"`.

**Recusa** (passo 9): repita os dois primeiros comandos do passo 7 e então responda `escolha=recusar`. A nova pausa oferece só `sala-fusca`, porque a mirante foi ocupada no passo 8. O resultado esperado é `TASK_STATE_CANCELED`.

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d @/tmp/ponte-08.json | tee /tmp/ponte-resp.json | python3 -m json.tool
TASK=$(python3 -c 'import json; print(json.load(open("/tmp/ponte-resp.json"))["result"]["task"]["id"])')
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":4,"method":"SendMessage","params":{"message":{"messageId":"msg-recusa-1","role":"ROLE_USER","taskId":"'"$TASK"'","parts":[{"text":"escolha=recusar"}]}}}' | python3 -m json.tool
```

**Sala inexistente** (passo 10). Esperado: `TASK_STATE_FAILED`, com `Sala inexistente: sala-inexistente` no status e no `history`.

```bash
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":5,"method":"SendMessage","params":{"message":{"messageId":"msg-inexistente-1","role":"ROLE_USER","parts":[{"text":"reservar sala=sala-inexistente inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]}}}' | python3 -m json.tool
```

**MRTR direto no servidor MCP** (passos 11 a 13). O primeiro comando define uma função `mcp` que envia um corpo JSON com os headers da revisão `2026-07-28` e mostra o status HTTP. Depois o roteiro:

1. pega a pausa com o corpo do `wire/03`;
2. monta dois retries a partir do `wire/04`: um com o `requestState` intacto e outro com um caractere trocado. A chave do `inputResponses` e a sala aceita vêm da própria pausa, e o `requestState` fica só nos arquivos, sem ir para a tela.

```bash
mcp() { curl -s -w '\nHTTP %{http_code}\n' http://localhost:7301/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: reservar_sala' -d @"$1"; }
python3 -c 'import json; print(json.dumps(json.load(open("exemplos/wire/03-tools-call-conflito-input-required.json"))["request"]["body"]))' > /tmp/ponte-conflito.json
mcp /tmp/ponte-conflito.json > /tmp/ponte-pausa.txt; tail -1 /tmp/ponte-pausa.txt
python3 - <<'EOF'
import json
pausa = json.loads(open("/tmp/ponte-pausa.txt").read().rsplit("\nHTTP", 1)[0])["result"]
chave, pedido = next(iter(pausa["inputRequests"].items()))
sala = pedido["params"]["requestedSchema"]["properties"]["sala"]["enum"][0]
corpo = json.load(open("exemplos/wire/04-tools-call-retry.json"))["request"]["body"]
corpo["params"]["inputResponses"] = {chave: {"action": "accept", "content": {"sala": sala}}}
estado = pausa["requestState"]
corpo["params"]["requestState"] = estado
json.dump(corpo, open("/tmp/ponte-retry.json", "w"))
i = len(estado) // 2
corpo["params"]["requestState"] = estado[:i] + ("A" if estado[i] != "A" else "B") + estado[i + 1:]
json.dump(corpo, open("/tmp/ponte-retry-adulterado.json", "w"))
print(f"resultType={pausa['resultType']} chave={chave} sala aceita={sala}")
EOF
```

Passo 11, `requestState` adulterado. Esperado: `-32602` e `HTTP 400`.

```bash
mcp /tmp/ponte-retry-adulterado.json
```

Passo 12: reinicie o servidor MCP (Ctrl+C no terminal 1 e `./subir-servidor-mcp.sh` de novo) e reenvie o retry intacto. Esperado: `"resultType":"complete"` com `"reservado":true` na sala aceita.

```bash
mcp /tmp/ponte-retry.json
```

Passo 13, o mesmo conflito com `clientCapabilities: {}`, que é o corpo do `wire/06`. Esperado: `-32021` e `HTTP 400`.

```bash
python3 -c 'import json; print(json.dumps(json.load(open("exemplos/wire/06-erro-32021-sem-elicitation.json"))["request"]["body"]))' > /tmp/ponte-06.json
mcp /tmp/ponte-06.json
```

### 5. Opcional: o agente só como cliente MCP

`agente/demo_mcp.py` faz o ciclo MRTR sem A2A, contra o servidor MCP no ar:

1. `tools/list`;
2. `resources/read`;
3. a pausa da garagem;
4. o retry com a escolha passada como argumento;
5. o erro da sala inexistente.

```bash
uv run --locked --project agente python agente/demo_mcp.py sala-fusca
```

```bash
MCP_DEBUG_HEADERS=1 uv run --locked --project agente python agente/demo_mcp.py recusar
```

Com `MCP_DEBUG_HEADERS=1`, cada request imprime no stderr os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`.

## Onde a ponte acontece

A ida acontece em `agente/agente_a2a.py`, no método `ExecutorDaPonte._tratar_resultado`.

1. O `ClienteMCP` devolve o `input_required` do servidor como uma `Pausa`, com chave, campo, alternativas do `enum` e o `requestState` opaco.
2. O executor monta uma `PausaGuardada` com os argumentos originais, a chave, as alternativas, a versão da política, o traceparent e o `requestState`.
3. Essa pausa vai para `self._pausas[task_id]`, fora da Task.
4. A Task passa para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...`.

A volta acontece em `ExecutorDaPonte._continuar`:

1. O executor valida `escolha=<valor>` contra as alternativas guardadas.
2. Ele chama `ClienteMCP.retomar` (`agente/cliente_mcp.py`), que repete o `tools/call` original:
   - com os mesmos argumentos;
   - com um id de JSON-RPC novo, único na vida do processo;
   - com `inputResponses` na mesma chave que veio em `inputRequests`;
   - com o `requestState` ecoado sem modificação.

O agente não abre, não decodifica e não reconstrói o `requestState`. Ele é uma `str` com `repr=False`, que não entra em log, no card, no artifact nem em mensagens A2A.

| A2A (cliente do agente) | MCP (servidor `central-de-salas`) |
|---|---|
| `SendMessage` com `reservar sala=... inicio=... fim=... responsavel=...` | `resources/read politica://uso` e `tools/list` (o primeiro só uma vez), depois `tools/call reservar_sala` |
| `TASK_STATE_INPUT_REQUIRED` com `alternativas: a, b`, na ordem do `enum` | `resultType: input_required`, elicitation em form mode com `enum` de salas |
| `escolha=<id do enum>` | retry com `inputResponses[chave] = {"action":"accept","content":{"sala":"<id>"}}` |
| `escolha=recusar` | retry com `inputResponses[chave] = {"action":"decline"}` |
| escolha fora do `enum` | nada vai ao MCP; a Task continua `INPUT_REQUIRED` com a mesma linha |
| `TASK_STATE_COMPLETED` com artifact `reserva` (JSON com `politica`) | `resultType: complete`, `reservado: true` |
| `TASK_STATE_CANCELED` | `resultType: complete`, `reservado: false`, `motivo: "recusado"` |
| `TASK_STATE_FAILED` com o texto da tool no status e no `history` | `isError: true` |
| nova pausa, que substitui a guardada | outro `input_required` no retry (a sala escolhida foi tomada no meio) |

**Trace.** O caminho do trace-id é este:

1. `ExecutorDaPonte._traceparent_do_header` lê o header `traceparent` do `SendMessage` que cria a Task. Ele mantém o trace-id e, se o header faltar ou for inválido, gera um novo.
2. Cada request MCP leva esse trace-id em `_meta.traceparent`, com um span-id novo a cada request (`_Trace.meta` em `cliente_mcp.py`).
3. O traceparent fica guardado em `PausaGuardada.traceparent`. Por isso o retry sai com o mesmo trace-id da pausa, mesmo que o `SendMessage` de continuação não traga header.

## Decisões técnicas

**Proteção do `requestState`.**
- O servidor usa o `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` do próprio SDK `mcp`.
- O estado é cifrado e autenticado com AES-256-GCM, com chave derivada do segredo por HKDF.
- O estado fica preso:
  - ao método;
  - à tool;
  - a um digest dos argumentos;
  - ao servidor (audience).
- Vale por 600 segundos.
- Adulterar o estado, trocar os argumentos no retry ou usar um estado vencido dá `-32602 Invalid or expired requestState`, com HTTP 400.
- O servidor não guarda nada entre a pausa e o retry: a sala pedida, o intervalo, o responsável e as alternativas oferecidas viajam selados no próprio estado. Por isso o retry funciona depois de um restart do servidor MCP, e o que vale são as alternativas seladas.
- O servidor se recusa a subir sem segredo ou com menos de 32 bytes, e nunca usa chave efêmera.

**`InputRequiredResult` próprio em vez de `Resolve()`.**
- `reservar_sala` devolve um `InputRequiredResult` montado à mão: elicitation em form com `enum` de até 3 salas, ordenadas por capacidade e depois por id.
- No retry, a tool lê `ctx.request_state` e `ctx.input_responses`.
- Com `Resolve()`, o SDK recalcularia a pergunta no retry a partir do estado atual. Se as reservas mudassem entre a pausa e o retry (drift), a pergunta do retry não seria mais a que o cliente respondeu.
- Com o estado selado, o retry valida a resposta contra exatamente as alternativas oferecidas. Uma resposta fora delas dá `-32602`. Se a sala escolhida foi tomada nesse meio-tempo, o servidor pergunta de novo, com alternativas atuais.
- A falta da capability `elicitation.form` dá `-32021` com HTTP 400. O servidor só faz essa checagem quando há de fato uma pergunta a fazer: sala livre reserva direto, e conflito sem alternativa dá `isError`.

**Onde fica o estado das Tasks.**
- As Tasks ficam no `TaskStore` em memória do a2a-sdk.
- O que é preciso para retomar fica num `dict` do executor (`ExecutorDaPonte._pausas`), indexado pelo id da Task e fora da Task: argumentos, chave, alternativas, política, traceparent e `requestState`. A entrada sai do `dict` quando a Task termina.
- Cada Task tem a sua entrada, então duas pausas simultâneas não trocam de estado.
- Se o agente reiniciar, as Tasks e as pausas se perdem; não há persistência, por decisão do enunciado. Se o servidor MCP reiniciar, nada se perde, porque o estado viaja no `requestState`.

**Capability só com form.** O agente declara exatamente `{"elicitation":{"form":{}}}`. No SDK `mcp` 2.3.0 (`mcp/client/session.py`, `ClientSession._build_capabilities`), a capability de elicitation só é anunciada quando há callback, e sempre com form e url:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

O carimbo do `_meta` sobrescreve esse valor em todo request (`meta[CLIENT_CAPABILITIES_META_KEY] = capabilities`), e não há parâmetro público para escolher os modos. Por isso `cliente_mcp.py` troca só o anúncio:

```python
class _SessaoSoForm(ClientSession):
    def _build_capabilities(self, version: str) -> ClientCapabilities:
        return ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
```

O callback registrado, `_elicitation_proibida`, é uma armadilha: se rodar, conta a chamada e levanta erro. Ele nunca roda, porque `call_tool(..., allow_input_required=True)` entrega o `input_required` cru e a pergunta volta ao cliente A2A. O `demo_mcp.py` imprime essa contagem, que dá 0.

**`A2A-Version` ausente tratado como 1.0.**
- O a2a-sdk 1.3.1 trata a falta do header como versão 0.3 (`a2a/utils/version_validator.py`: `if not actual_version: return constants.PROTOCOL_VERSION_0_3`) e recusa `SendMessage` e `GetTask` com "Version mismatch".
- O validador e os `exemplos/wire/08` a `10` não mandam esse header.
- O `enable_v0_3_compat` não resolve, porque roteia pelo nome do método.
- `ContextoVersao10PorPadrao`, ligado pelo ponto de extensão público `context_builder` de `create_jsonrpc_routes`, assume 1.0 quando o header falta. 1.0 é a única versão declarada no card.
- Um cliente que declare outra versão explicitamente continua recusado.

**`TaskStoreComHistorico`.** O SDK só move `status.message` para o `history` quando chega o status seguinte. Assim, a mensagem final de uma Task terminal, como o erro exato da tool num `FAILED`, nunca entraria no `history`. Este `InMemoryTaskStore` estendido acrescenta a mensagem de status ao `history` ao salvar e descarta repetidas pelo `messageId`. O resultado tem a forma dos `wire/08` e `10`.

**Reconexão do `ClienteMCP` e ids únicos.**
- Um único `ClienteMCP` vive durante todo o processo do agente. A conexão (httpx, transporte Streamable HTTP e `ClientSession`) fica numa task própria.
- No SDK 2.3.0, uma falha de rede estoura o task group do transporte. Isolada nessa task, a falha derruba só a conexão: a Task em andamento termina `FAILED` com "Falha ao falar com o servidor MCP: ...", e o request seguinte reconecta.
- Cada reconexão cria um `JSONRPCDispatcher` novo, e o do SDK numera a partir de 1. Isso faria o retry de uma Task pausada antes da queda repetir o id do `tools/call` que pausou.
- `_DispatcherIdsDoProcesso` sobrescreve só `_allocate_id` com um contador do processo inteiro. O `call_tool` não aceita `request_id`.
- Na sessão nova, o agente refaz o `tools/list`, com o traceparent da operação, antes do próximo `tools/call`. Sem isso, o próprio SDK faria um `tools/list` sem `_meta` para validar o `outputSchema`.

**Diferenças em relação aos wires** (todas aceitas pelo validador ou pelo enunciado):
- `GetTask` devolve a Task direto em `result`, como faz o SDK e como manda a spec A2A v1.0. O `wire/09` a embrulha em `result.task`. O `SendMessage` vem em `result.task`.
- Os ids de Task, mensagem e artifact são UUIDs gerados pelo SDK, sem os prefixos `task-` e `msg-`.
- O texto de erro de execução traz o prefixo que o SDK do servidor acrescenta: `Error executing tool reservar_sala: Sala inexistente: sala-inexistente`.
- A chave da elicitation é `escolha_de_sala`, sem o prefixo de módulo `__main__:` que aparece nos wires. O agente usa a chave que vier no `inputRequests`.

**Versões travadas.**
- Servidor: `mcp==2.3.0`, `pydantic==2.14.4`, `uvicorn==0.54.0`.
- Agente: `a2a-sdk[http-server]==1.3.1`, `mcp==2.3.0`, `uvicorn==0.54.0`, e `httpx2` e `starlette` com versão exata por faixa de Python. O `uv` resolve versões diferentes para 3.10 e para 3.11 ou superior, e as duas estão no lock.
- `requires-python = ">=3.10"` nos dois projetos. Os scripts usam `uv run --locked`, que falha em vez de atualizar o lock.
- O validador também passa com o agente rodando em Python 3.10.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: ba1f43f786595477b8d0a1f70d192cc6
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Código de saída: `0`.
