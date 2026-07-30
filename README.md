# EVChargerSim

Simulador de Charge Point OCPP 1.6J (mobilityhouse/ocpp) — simula o lado
carro/carregador de um ponto AC genérico, conectando no seu CSMS real via
WebSocket, pra testar a lógica do servidor sem hardware físico.

Roda direto no painel de controle web: liga sem nenhum charger, você
adiciona/remove quantos quiser e controla cada um (start/stop/pause/
resume/fault/clear/disconnect) dali, em tempo real — sem precisar saber
de antemão quantos vai usar nem reiniciar o processo pra mudar isso.

## Estrutura

```
evchargersim/
├── __init__.py         # docstring do pacote, exporta SimConfig e main
├── __main__.py         # ponto de entrada de `python -m evchargersim`
├── config.py           # SimConfig, FAULT_CODE_MAP, parsing de CLI
├── state.py             # ChargerState — estado de runtime de UM charger
├── physics.py            # funções puras de simulação (tensão, corrente)
├── logging_utils.py      # logger colorido do projeto (1 logger por charger)
├── charger.py            # EVChargerSim — a classe do protocolo em si
├── control_panel.py      # servidor HTTP do painel de controle
├── orchestrator.py       # ciclo de vida de conexão/reconexão + main()
└── frontend/              # painel de controle web — HTML/CSS/JS separados,
    ├── index.html          # sem build step, servidos estaticamente por
    ├── style.css           # control_panel.py
    └── app.js
```

## Instalação

```bash
pip install ocpp websockets
```

## Uso — modo padrão (painel de controle web)

De dentro da pasta que contém `evchargersim/`:

```bash
python -m evchargersim --url ws://seu-csms:9001
```

Abre em `http://localhost:8080` sem nenhum charger — digite um ID no
campo do topo e clique em "+ Adicionar charger". Cada card mostra
status/SoC/energia/corrente/fila offline ao vivo (atualização por push
via Server-Sent Events, não polling — ver `/api/events`) e tem botões
pra start/stop/pause/resume/fault/clear/disconnect/remover. Chargers
podem ser adicionados e removidos a qualquer momento, com o processo já
rodando.

A faixa "Todos" no topo do painel dispara start/stop/pause/resume/
disconnect/fault/clear em todos os chargers **visíveis no momento**
(respeita o filtro de busca — filtre por prefixo e a ação em massa afeta
só aqueles). "+ Adicionar" tem um botão de opções avançadas (⚙) pra
definir bateria/SoC inicial/corrente padrão só pros IDs daquela leva.

Se quiser já subir com alguns pré-carregados (opcional, você pode
adicionar/remover mais depois de qualquer forma):

```bash
python -m evchargersim --fleet CH01,CH02,CH03 --url ws://seu-csms:9001 --control-port 8080
```

## Uso — modo legado (1 charger, console de texto, sem painel)

```bash
python -m evchargersim --console CARREGADOR_02 --url ws://seu-csms:9000
```

Console de texto disponível (`help` pra ver a lista completa de comandos:
start/stop/pause/resume/fault/clear/datatransfer/queue/authcache/locallist/disconnect).

## Verbosidade do terminal

Por padrão o terminal fica enxuto: só eventos de fato (start/stop/
fault/reset/comandos remotos/etc.) aparecem em INFO. As linhas
periódicas — Heartbeat e a amostra de MeterValues a cada ciclo (padrão
30s) — ficam em DEBUG e não aparecem sozinhas, já que se repetem por
charger a cada ciclo e em modo frota com vários chargers dominavam o
terminal sem agregar informação nova.

Pra ver esse detalhamento completo (útil pra depurar tensão/corrente/
SoC ciclo a ciclo), suba com `--verbose`:

```bash
python -m evchargersim --verbose --url ws://seu-csms:9001
```

## Persistência da frota entre reinícios

Por padrão, reiniciar o processo esquece quais chargers estavam
rodando — a frota some, mesmo que cada charger em si vá voltar a
existir se você readicionar o mesmo ID (a sessão dele é sempre nova,
como um charger de verdade que perdeu energia: SoC/energia/fila offline
nunca são persistidos, só a *lista* de quais IDs devem existir).

```bash
python -m evchargersim --persist-file ./frota.json --url ws://seu-csms:9001
```

A lista é salva a cada adição/remoção (escrita atômica) e recarregada
no próximo boot, somada a qualquer `--fleet` passado nessa execução.

## Autenticação do painel

Sem `--control-token`, o painel roda sem autenticação (como antes) —
qualquer um na rede que alcance a porta consegue start/stop/remover
chargers. Com o flag, todo request a `/api/*` exige o token:

```bash
python -m evchargersim --control-token "um-segredo-qualquer" --url ws://seu-csms:9001
```

No navegador, clique no 🔒 no topo do painel e cole o token — ele fica
salvo no `localStorage` da aba. Os arquivos estáticos (`/`, `/app.js`,
`/style.css`) nunca exigem token; só a API.

## Encerramento gracioso

`Ctrl+C` (SIGINT) ou `kill` sem `-9` (SIGTERM) agora fecham a conexão
WebSocket de cada charger de forma limpa (frame de close de verdade)
e derrubam o painel web antes do processo sair, em vez de simplesmente
matar tudo no meio. Um segundo Ctrl+C/SIGTERM durante esse processo
força saída imediata, pro caso de algo travar no meio do shutdown.

## Testando robustez do CSMS

```bash
python -m evchargersim --chaos-disconnect-interval 30 --chaos-drop-rate 0.1
```

Ver `--help` pra lista completa de flags de chaos (latência, drop rate,
intervalo de desconexão) — funcionam em qualquer modo.

## Notas desta revisão do modo frota/dashboard

Testado de ponta a ponta com um CSMS mock: chargers adicionados/removidos
dinamicamente pelo painel enquanto o processo já está rodando (sem
reinício), duplicata de ID recusada, remover ID inexistente retorna 404
sem quebrar nada, re-adicionar um ID já removido funciona normalmente, e
uma checagem de contagem de tasks confirma que remover um charger encerra
de fato TODOS os loops de fundo dele (heartbeat/meter values/acumulador/
chaos) — não deixa nada órfão rodando contra uma conexão já fechada.

Também testado: N chargers conectando/bootando em paralelo, comandos
concorrentes disparados em vários ao mesmo tempo (sessões isoladas,
`transaction_id` sempre distinto por charger, sem cross-talk),
double-start no mesmo charger disparado em paralelo (só a primeira
tentativa vence), e chaos `disconnect` num charger confirmando que os
demais não são afetados e que ele reconecta sozinho.

Dois bugs reais foram encontrados e corrigidos nesse processo:

1. **Loggers compartilhados entre chargers** — `build_logger()` usava
   `logging.basicConfig()` + `logging.getLogger("evchargersim")`, ambos
   singletons de processo; com múltiplos chargers, todos acabavam
   compartilhando o mesmo objeto de logger e só o primeiro tinha seu ID
   de fato aplicado ao formatter. Corrigido: cada charger tem seu
   próprio logger nomeado (`evchargersim.<charge_point_id>`).

2. **Vazamento de tasks de fundo ao remover um charger** — cancelar a
   task de ciclo de vida de um charger não cancelava os loops que ela
   próprio subiu (heartbeat/meter values/acumulador/chaos), que ficavam
   órfãos rodando pra sempre. Corrigido: `run_charger_lifecycle` agora
   rastreia e cancela essas tasks num `finally` ao sair de cena.

## Notas da revisão de frontend/logging

- **Ícone de raio removido do cabeçalho** (`frontend/index.html`) — o
  `brand-mark` (⚡) ao lado do título "EVChargerSim" foi retirado do
  painel de controle web.
- **Terminal menos verboso por padrão** — a linha de MeterValues do
  `send_meter_values_loop` (com ou sem sessão ativa), antes em INFO a
  cada ciclo, foi rebaixada para DEBUG, no mesmo nível do Heartbeat.
  Ver [Verbosidade do terminal](#verbosidade-do-terminal) acima.

## Notas da revisão de backend/frontend (SSE, shutdown, persistência, auth, UX)

**Backend**

- **SSE em vez de polling** (`/api/events`) — o painel empurra um novo
  snapshot só quando o estado muda de verdade (comparação por
  igualdade do JSON), com keepalive a cada 15s pra sobreviver a
  proxies. `/api/state` continua existindo como fallback avulso.
- **Encerramento gracioso** (`__main__.py`) — SIGINT/SIGTERM cancelam a
  task principal em vez de matar o processo; cada charger fecha a
  conexão WebSocket de forma limpa antes de sair.
- **Persistência da lista de frota** (`--persist-file`) — sobrevive a
  reinícios; estado de sessão continua efêmero de propósito.
- **Autenticação opcional** (`--control-token`) — protege `/api/*` via
  header `Authorization: Bearer` ou `?token=` (necessário pro
  `EventSource`, que não manda headers customizados).
- **Overrides de config por charger** — `POST /api/chargers` aceita
  campos extras (whitelist em `CHARGER_OVERRIDE_FIELDS`, `config.py`)
  pra dar bateria/SoC inicial/corrente diferentes a cada charger.
- **`/api/command/all` aceita `ids`** — sem isso, "todos" sempre batia
  no registry inteiro; agora o painel manda só os IDs visíveis
  (respeita o filtro de busca).

**Frontend**

- Ações em massa (Start/Stop/Pausar/Retomar/Desconectar/Fault/Clear)
  agora respeitam o filtro de busca, mostram contagem
  ("Visíveis (N):") e desabilitam o botão clicado enquanto a
  requisição está em voo. Stop e Fault em massa pedem confirmação.
- Toast de ação em massa tem um "ver detalhes" expansível com a
  mensagem de cada charger, em vez de só o resumo.
- `toast-stack` ganhou `aria-live="polite"` pra leitor de tela anunciar
  as mensagens.
- Grade de cards ganhou ordenação (ID/Status/SoC) via `#sort-select`,
  reordenando na hora a partir do último snapshot conhecido.
- Indicador de conexão do SSE (bolinha ao lado do subtítulo) e botão
  🔒 pra configurar o token de acesso, salvo em `localStorage`.
