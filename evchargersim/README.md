# EVChargerSim

Simulador de Charge Point OCPP 1.6J ([mobilityhouse/ocpp](https://github.com/mobilityhouse/ocpp)) — simula o lado carro/carregador de um ponto de recarga AC genérico, conectando no seu CSMS real via WebSocket para testar a lógica do servidor sem hardware físico.

Sobe direto um painel de controle web: liga sem nenhum charger, você adiciona/remove quantos quiser e controla cada um em tempo real, sem reiniciar o processo.

## Instalação

```bash
pip install ocpp websockets
```

## Início rápido

De dentro da pasta que contém `evchargersim/`:

```bash
python -m evchargersim --url ws://seu-csms:9001
```

Abre em `http://localhost:8080`. Digite um ID no campo do topo, clique em "Adicionar" — pronto, o charger conecta no CSMS e aparece na frota.

## Funcionalidades

**Painel web (frota)**
- Adicionar/remover chargers a qualquer momento, sem reiniciar o processo
- Cada card: status, SoC, energia, corrente, fila offline — atualizado ao vivo via Server-Sent Events (`/api/events`), sem polling
- Ações por charger: start/stop/pause/resume/fault/clear/disconnect
- Ações em massa (respeitam o filtro de busca ativo), com confirmação para stop/fault
- Import de uma lista de IDs a partir de um arquivo `.txt` (um por linha ou separados por vírgula)
- Opções avançadas por leva: bateria, SoC inicial e corrente padrão só para os IDs adicionados naquele momento
- Alternância de tema claro/escuro, persistida no navegador
- Tema visual: cores/vocabulário de painel elétrico industrial (LED de conector, forma de onda CA ao vivo, gauge de SoC segmentado)

**Aba Histórico**
- Gráfico ampliado por charger: SoC, corrente real e limite ofertado pelo CSMS ao longo do tempo
- Modo "Comparar": sobrepõe a mesma métrica (SoC, corrente real, limite ofertado ou potência) de até 8 chargers num único gráfico, alinhados pelo tempo real

**Protocolo (OCPP 1.6J)**
- Fluxo completo de transação (Authorize/StartTransaction/MeterValues/StopTransaction), Smart Charging com stacking de `TxProfile`, `GetCompositeSchedule`, `ClearChargingProfile` seletivo
- Reserva (`ReserveNow`/`CancelReservation`), Local Authorization List, Authorization Cache
- Reconexão automática com backoff exponencial; sessão continua rodando fisicamente enquanto offline, mensagens não entregues ficam numa fila local reenviada em ordem ao reconectar
- Instabilidade de rede injetável (desconexão periódica, latência, perda de mensagem) para testar a robustez do CSMS
- Limites físicos configuráveis (corrente máxima de hardware, períodos de schedule, perfis simultâneos) de fato aplicados, não só anunciados

## Estrutura do projeto

```
evchargersim/
├── __init__.py         # docstring do pacote, exporta SimConfig e main
├── __main__.py         # ponto de entrada de `python -m evchargersim`
├── config.py            # SimConfig, FAULT_CODE_MAP, parsing de CLI
├── state.py              # ChargerState — estado de runtime de UM charger
├── physics.py             # funções puras de simulação (tensão, corrente)
├── logging_utils.py       # logger colorido do projeto (1 logger por charger)
├── charger.py             # EVChargerSim — a classe do protocolo em si
├── control_panel.py       # servidor HTTP do painel de controle
├── orchestrator.py        # ciclo de vida de conexão/reconexão + main()
└── frontend/
    ├── index.html          # painel de controle — sem build step,
    ├── style.css           # servidos como arquivos estáticos por
    ├── app.js              # control_panel.py
    ├── pure.js             # lógica sem DOM, testável isoladamente
    └── tests/
        └── pure.test.js    # suíte de testes (node --test tests/)
```

## Modos de uso

**Painel web (padrão)** — modo frota, zero chargers no boot:

```bash
python -m evchargersim --url ws://seu-csms:9001
```

Se quiser já subir com alguns pré-carregados (opcional):

```bash
python -m evchargersim --fleet CH01,CH02,CH03 --url ws://seu-csms:9001
```

**Console de texto (legado)** — um único charger, sem painel web:

```bash
python -m evchargersim CARREGADOR_02 --url ws://seu-csms:9001 --console
```

Comandos disponíveis (`help` no console): `start` `stop` `pause` `resume` `fault` `clear` `datatransfer` `queue` `authcache` `locallist` `disconnect`.

## Referência de flags (CLI)

| Flag | Padrão | Descrição |
|---|---|---|
| `charge_point_id` | `EVCHARGERSIM_01` | ID do charge point (posicional, modo `--console`) |
| `--url` | `ws://localhost:9001` | URL base do CSMS, sem o ID |
| `--config` | — | Arquivo JSON com valores padrão (CLI tem prioridade) |
| `--fleet` | — | Lista de IDs separados por vírgula pra já subir pré-carregados |
| `--console` | desligado | Modo legado: 1 charger, console de texto, sem painel |
| `--control-port` | `8080` | Porta HTTP do painel web |
| `--control-token` | — | Se definido, exige esse token em todo request a `/api/*` |
| `--persist-file` | — | Arquivo JSON pra lembrar a lista de chargers entre reinícios |
| `--verbose` | desligado | Mostra Heartbeat/MeterValues no terminal |
| `--connector-id` | `1` | ID do conector |
| `--meter-interval` | `30` | Intervalo de MeterValues (segundos) |
| `--heartbeat-interval` | `120` | Intervalo inicial de Heartbeat (segundos) |
| `--default-amps` | `16.0` | Corrente ao iniciar sessão, antes do 1º `SetChargingProfile` |
| `--sim-speed` | `1.0` | Fator de aceleração da simulação |
| `--battery-wh` | `50000` | Capacidade da bateria simulada (Wh) |
| `--initial-soc` | `20.0` | SoC inicial de cada sessão (%) |
| `--voltage` | `225.0` | Tensão nominal, fase-neutro (V) |
| `--phases` | `1` | Número de fases (`1`, `2` ou `3`) |
| `--power-factor` | `1.0` | Fator de potência da carga (< 1.0 adiciona componente reativa) |
| `--hardware-max-amps` | `32.0` | Teto físico de corrente, de fato aplicado |
| `--max-schedule-periods` | `10` | Períodos de `chargingSchedule` que o charger guarda |
| `--max-tx-profiles` | `3` | `TxProfile`s simultâneos aceitos (um por `stackLevel`) |
| `--call-timeout` | `30.0` | Timeout (s) para Start/StopTransaction |
| `--max-offline-queue` | `500` | Teto da fila offline por charger (`0` = sem limite) |
| `--chaos-disconnect-interval` | desligado | Derruba o WebSocket a cada N segundos ± jitter |
| `--chaos-disconnect-jitter` | `5.0` | Variação (± segundos) em torno do intervalo acima |
| `--chaos-latency-min` / `--chaos-latency-max` | `0` | Atraso artificial por mensagem (ms) |
| `--chaos-drop-rate` | `0.0` | Probabilidade (0.0–1.0) de perda simulada de mensagem |

`--hardware-max-amps`, `--max-schedule-periods`, `--max-tx-profiles`, `--power-factor`, `--phases`, `--battery-wh`, `--initial-soc` e `--voltage` também podem ser definidos por charger individual em modo frota, via `POST /api/chargers` (útil pra simular uma frota com capacidades físicas diferentes).

## API HTTP (painel de controle)

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/state` | Snapshot de todos os chargers registrados |
| `GET` | `/api/events` | Stream SSE — mesmo snapshot, empurrado a cada mudança de estado |
| `GET` | `/api/history/<id>` | Amostras de histórico (SoC/corrente/potência/energia) de um charger |
| `POST` | `/api/chargers` | Adiciona um charger — `{"charge_point_id": "CH01", ...overrides}` |
| `DELETE` | `/api/chargers/<id>` | Remove um charger |
| `POST` | `/api/chargers/<id>/chaos` | Ajusta chaos de um charger já conectado, em tempo real |
| `POST` | `/api/chargers/<id>/phases` | Ajusta o número de fases de um charger já conectado |
| `POST` | `/api/command` | Executa um comando local — `{"charge_point_id", "cmd", "args"}` |
| `POST` | `/api/command/all` | Executa um comando em todos os chargers (ou só num subconjunto via `ids`) |

Com `--control-token` definido, toda rota `/api/*` exige o token via header `Authorization: Bearer <token>` ou querystring `?token=<token>` (necessário só para `/api/events`, já que o `EventSource` do navegador não manda headers customizados). Os arquivos estáticos (`/`, `/app.js`, `/pure.js`, `/style.css`) nunca exigem token.

## Testes

O JavaScript sem dependência de DOM (`frontend/pure.js`) tem sua própria suíte em `frontend/tests/pure.test.js`, sem build step:

```bash
cd evchargersim/frontend
node --test
```

## Notas operacionais

- **Persistência** (`--persist-file`) salva só a *lista* de IDs, não o estado de sessão — cada charger volta com SoC/energia/fila zerados ao reiniciar, como um charger de verdade que perdeu energia.
- **Encerramento gracioso** — `Ctrl+C`/`SIGTERM` fecham a conexão WebSocket de cada charger de forma limpa e derrubam o painel antes do processo sair. Um segundo sinal força saída imediata.
- **Reconexão** não reenvia `BootNotification` — só resincroniza o status atual e esvazia a fila offline. Uma sessão que estava carregando continua sob o mesmo `transaction_id`.
- **Token de acesso** protege contra acesso casual na rede, não substitui uma rede fechada — o token de `/api/events` trafega por querystring (limitação do `EventSource`), então pode acabar em access logs de proxies na frente do painel.
