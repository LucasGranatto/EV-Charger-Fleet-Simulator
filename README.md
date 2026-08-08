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

O painel tem duas abas: **Frota** (a grade de cards acima, aberta por
padrão) e **Histórico** — o gráfico de SoC/corrente/limite ofertado de
cada charger, ampliado. Antes esse gráfico vivia espremido dentro do
próprio card; agora clicar no ícone 📈 de qualquer card leva direto pra
essa aba com aquele charger já selecionado (ou troque de charger por
lá mesmo, pelo seletor/setas ⟨ ⟩ no topo da aba).

A faixa "Todos" no topo do painel dispara start/stop/pause/resume/
disconnect/fault/clear em todos os chargers **visíveis no momento**
(respeita o filtro de busca — filtre por prefixo e a ação em massa afeta
só aqueles). "+ Adicionar" tem um botão de opções avançadas (⚙) pra
definir bateria/SoC inicial/corrente padrão só pros IDs daquela leva, e
um botão de import (⇪) pra carregar uma lista de IDs de um arquivo
`.txt` de uma vez — ver [Importar chargers de um arquivo](#importar-chargers-de-um-arquivo) abaixo.

Se quiser já subir com alguns pré-carregados (opcional, você pode
adicionar/remover mais depois de qualquer forma):

```bash
python -m evchargersim --fleet CH01,CH02,CH03 --url ws://seu-csms:9001 --control-port 8080
```

## Importar chargers de um arquivo

Pra adicionar muitos chargers de uma vez sem digitar ID por ID, clique
no botão de import (⇪) ao lado de "+ Adicionar" e escolha um `.txt`.
Formato aceito:

```
CH01
CH02
CH03
```

ou tudo numa linha separado por vírgula (`CH01, CH02, CH03`) — os dois
formatos podem ser misturados no mesmo arquivo. Linhas em branco são
ignoradas e IDs duplicados no arquivo são descartados automaticamente
(mantendo a 1ª ocorrência).

O arquivo é lido inteiramente no navegador — nenhum upload ao servidor,
nenhuma rota nova: cada ID vira exatamente o mesmo `POST /api/chargers`
que "+ Adicionar" já dispara, um por um. Por isso, se o botão de opções
avançadas (⚙) estiver aberto com bateria/SoC inicial/corrente padrão
definidos, esses overrides valem pra toda a leva importada também. Ao
final, um toast resume quantos entraram e — se algum falhar (ex: ID já
existente no registry) — lista o detalhe de cada um.

Essa importação só existe no painel web, por enquanto; não há
equivalente em `--fleet`/CLI (que continua aceitando só uma lista
direto na linha de comando).

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

## Limites físicos e Smart Charging

Todo charger simulado anuncia (via `GetConfiguration`) e **aplica de
fato** dois tetos que um charger real também teria:

```bash
python -m evchargersim --hardware-max-amps 32 --max-schedule-periods 10 --max-tx-profiles 3 --url ws://seu-csms:9001
```

- `--hardware-max-amps` (padrão: 32) — teto físico de corrente da
  fiação/breaker simulados. Anunciado na chave `CurrentMax` e
  **clampado de verdade** em qualquer corrente oferecida — corrente
  padrão da sessão, ou um `SetChargingProfile` do CSMS pedindo mais do
  que isso. Um charger real não entrega mais corrente do que seu
  hardware suporta, não importa o que o CSMS peça; este simulador
  agora também não.
- `--max-schedule-periods` (padrão: 10) — quantos períodos de um
  `chargingSchedule` este charger de fato guarda (memória de firmware
  limitada). Anunciado na chave `ChargingScheduleMaxPeriods` e um
  `SetChargingProfile` com mais períodos que isso tem o excedente
  truncado, não silenciosamente aceito e ignorado por completo.
- `--max-tx-profiles` (padrão: 3) — quantos perfis `TxProfile`
  simultâneos (um por `stackLevel`) este charger aceita ter instalados
  ao mesmo tempo. Ver [stacking de TxProfile](#limites-físicos-e-smart-charging)
  abaixo.

Todos podem ser configurados por charger individual em modo frota (via
`POST /api/chargers`, mesma whitelist de `battery_capacity_wh`/
`nominal_voltage` — útil pra simular uma frota com chargers de
capacidades físicas diferentes, ex: alguns 16A, outros 32A).

`GetConfiguration` também passou a expor as 4 chaves padrão OCPP 1.6 de
capacidade do feature profile `SmartCharging`
(`ChargeProfileMaxStackLevel`, `ChargingScheduleAllowedChargingRateUnit`,
`ChargingScheduleMaxPeriods`, `MaxChargingProfilesInstalled`) — antes
`SupportedFeatureProfiles` anunciava `SmartCharging` sem nenhuma delas,
uma alegação que um CSMS não tinha como verificar.

**Stacking de `TxProfile`** — até `--max-tx-profiles` (padrão: 3)
perfis `TxProfile` simultâneos, um por `stackLevel` distinto, com o de
maior `stackLevel` vencendo a qualquer instante (spec: *"Higher values
have precedence over lower values"*). Um `SetChargingProfile` com um
`stackLevel` inédito além do teto é `Rejected`; o mesmo `stackLevel` já
instalado é atualizado/substituído normalmente (não conta como novo).
Um `TxProfile` amarrado a um `transactionId` que não é o da sessão
ativa também é `Rejected`. `ClearChargingProfile` respeita os critérios
opcionais da spec (`id`/`chargingProfilePurpose`/`stackLevel`) — sem
nenhum, limpa tudo; com eles, limpa só o que casa, sem afetar os outros
perfis instalados. `TxProfile` é escopado à transação por definição: é
limpo automaticamente no início de cada sessão nova e no fim da
sessão em que foi definido, não sobrevive entre transações.

`ChargePointMaxProfile` e `TxDefaultProfile` continuam sem stacking
entre si — um perfil "de fundo" de cada vez, substituído por completo
a cada `SetChargingProfile` novo desse purpose — mas um `TxProfile`
sempre tem precedência sobre eles quando ambos estão ativos.

E o handler de `GetCompositeSchedule` — que estava totalmente ausente —
agora responde com o efeito líquido do perfil ativo no conector
(`Rejected` se não há sessão, já que não há nada a compor). É o comando
que confirma na prática se um charger faz Smart Charging de verdade, em
vez de só confiar no que `SupportedFeatureProfiles` alega.

## Testando robustez do CSMS

```bash
python -m evchargersim --chaos-disconnect-interval 30 --chaos-drop-rate 0.1
```

Ver `--help` pra lista completa de flags de chaos (latência, drop rate,
intervalo de desconexão) — funcionam em qualquer modo.

Numa queda simulada por `--chaos-disconnect-interval`, a reconexão **não
reenvia `BootNotification`** — só esvazia a fila offline e resincroniza
o `StatusNotification` atual. Uma sessão que estava carregando continua
carregando sob o mesmo `transaction_id` depois de reconectar, do jeito
que um charger real se comporta (a transação não é amarrada à conexão
WebSocket). `BootNotification` sai só uma vez de verdade, no boot do
processo — reenviá-lo em toda reconexão de rede fazia alguns CSMS
tratarem a volta como um reboot físico e perderem a transação em
andamento, mesmo sem nenhum `StopTransaction` ter sido enviado.

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

## Notas da revisão de import de frota via arquivo

- **Botão de import (⇪) no painel web** — lê um `.txt` inteiro no
  browser (`file.text()`, sem upload ao servidor) e adiciona cada ID
  encontrado como um charger novo. Aceita um ID por linha e/ou
  separados por vírgula, ignora linhas em branco e remove duplicatas.
  Nenhuma rota nova: cada ID vira o mesmo `POST /api/chargers` que
  "+ Adicionar" já disparava.
- A lógica de "adicionar vários IDs" (antes só dentro do handler do
  campo de texto) foi extraída pra uma função compartilhada, usada
  tanto pelo campo digitado quanto pelo import de arquivo — overrides
  de "opções avançadas" (bateria/SoC/corrente) continuam se aplicando
  à leva inteira nos dois casos.
- Só existe no painel web por enquanto — sem equivalente em `--fleet`/
  CLI. Ver [Importar chargers de um arquivo](#importar-chargers-de-um-arquivo).

## Notas da revisão do gráfico de histórico e indicadores de chaos

- **Gráfico do card reformulado** — grade horizontal na escala de SoC
  (0/25/50/75/100%) com rótulos de eixo, escala de corrente rotulada no
  eixo direito, marcação de tempo no eixo X, área preenchida sob a
  linha de SoC, e uma 3ª série (limite ofertado pelo CSMS, tracejada) ao
  lado da corrente real — o vão entre as duas mostra visualmente se o
  carro está sendo limitado pelo CSMS ou é o tapering da própria
  bateria, sem precisar ler nenhum número.
- **Indicadores de chaos corrigidos** — o dot âmbar de "chaos ativo" no
  ícone do card foi removido; ele ficava aceso o tempo todo mesmo sem
  nenhum chaos de verdade ligado, porque a checagem antiga contava
  `max_offline_queue_size` (teto de fila, default 500) como se fosse
  instabilidade ativa. No lugar: um badge neutro com a contagem de
  grupos de chaos realmente ativos (desconexão/latência/perda), um
  resumo em texto no painel expandido ("Ativo agora: ..."), e destaque
  visual nos campos do formulário que pertencem a um grupo em efeito.

## Notas da revisão de UI (header, grid de fundo, aba Histórico)

- **Header reformulado** — virou uma barra elevada própria (cartão com
  borda, sombra e um trilho teal de destaque no topo), com o ícone da
  marca de volta num badge/emblema (supersede a nota "ícone de raio
  removido" de uma revisão anterior) e um selo mono "OCPP 1.6J" na
  subtitle, separado por um divisor vertical do indicador de conexão.
- **Grid de fundo em dois níveis** — trocada a grade única e uniforme
  por uma grade tipo blueprint: linhas finas a cada 17px quase
  imperceptíveis, e linhas um pouco mais visíveis a cada 68px marcando
  a escala maior, mais uma vinheta radial suave nos cantos. Mais
  detalhe, mas mais sutil no todo.
- **Aba "Histórico"** — o gráfico de cada charger saiu de dentro do
  card (pequeno demais pra leitura fina) e ganhou uma aba própria, com
  o mesmo SVG/lógica de renderização reaproveitados num container bem
  maior (o `viewBox` escala tudo — grade, texto, linhas —
  proporcionalmente, sem precisar de uma versão "grande" separada do
  código). O botão 📈 de cada card agora navega pra lá em vez de
  expandir algo inline; como só existe um gráfico visível por vez, o
  antigo `Map` de pollers por card virou um poller único.

## Notas da revisão de protocolo (reconexão, TriggerMessage, reboot)

Motivada por um CSMS real que não reconhecia uma sessão ainda ativa
depois de uma queda simulada por `--chaos-disconnect-interval`:

- **Reconexão de transporte não reenvia mais `BootNotification`** — só
  esvazia a fila offline e resincroniza o `StatusNotification` atual.
  Ver [Testando robustez do CSMS](#testando-robustez-do-csms) acima.
  Um caso de borda foi tratado junto: se a conexão cai *antes* do boot
  inicial ser aceito (ainda não existe registro nenhum do lado do
  CSMS), a reconexão volta a tentar o boot normalmente em vez de pular
  pro resync.
- **Hard reset e firmware update também mandavam `BootNotification`
  uma única vez**, sem confirmar `Accepted` — o mesmo problema, só que
  num lugar onde reenviar o Boot é o comportamento certo (reboot de
  verdade, ao contrário de um blip de rede). Os dois agora usam um
  helper compartilhado (`_simulate_reboot`) que tenta até o CSMS
  aceitar, e aborta a sequência (sem `Available`/`Installed`
  prematuros) se a conexão cair no meio.
- **`TriggerMessage` respondia `Accepted` pra tipos que não faziam
  nada** — de 6 valores válidos (`BootNotification`,
  `DiagnosticsStatusNotification`, `FirmwareStatusNotification`,
  `Heartbeat`, `MeterValues`, `StatusNotification`), só 3 eram
  tratados; os outros 3 recebiam "ok, vou mandar" e nada chegava.
  `BootNotification` agora é tratado de verdade; os dois de
  Diagnostics/Firmware — que só existem como parte de um fluxo já em
  andamento — respondem `NotImplemented`, a resposta que a spec já
  prevê pra isso.

## Notas da revisão de Smart Charging e limites físicos

Ver [Limites físicos e Smart Charging](#limites-físicos-e-smart-charging)
acima para o uso. Resumo do que mudou:

- Novos `--hardware-max-amps` (padrão 32), `--max-schedule-periods`
  (padrão 10) e `--max-tx-profiles` (padrão 3) em `SimConfig`,
  configuráveis também por charger individual em modo frota.
- `GetConfiguration` passou a expor `CurrentMax` (teto físico de
  corrente) e as 4 chaves padrão de capacidade `SmartCharging`
  (`ChargeProfileMaxStackLevel`, `ChargingScheduleAllowedChargingRateUnit`,
  `ChargingScheduleMaxPeriods`, `MaxChargingProfilesInstalled`) — antes
  nenhuma das duas existia, então um CSMS que depende delas pra saber o
  limite físico real (em vez de assumir um valor arbitrário) ou pra
  confirmar a capacidade de Smart Charging anunciada não tinha como.
- Os limites agora são **aplicados de verdade**, não só anunciados:
  `_apply_offered_amps()` clampa qualquer corrente oferecida acima de
  `hardware_max_amps`, e `on_set_charging_profile` trunca perfis com
  mais períodos que `max_schedule_periods`.
- Handler de `GetCompositeSchedule` implementado (estava totalmente
  ausente) — devolve o efeito líquido do perfil ativo no conector.
- **`TxProfile` passou a empilhar de verdade** — até `max_tx_profiles`
  perfis simultâneos, um por `stackLevel`, com o de maior `stackLevel`
  vencendo a qualquer instante (`_recompute_tx_profile_effective_amps`).
  Antes QUALQUER `SetChargingProfile`, de qualquer purpose, substituía
  o único perfil ativo por completo — `ChargeProfileMaxStackLevel` e
  `MaxChargingProfilesInstalled` chegaram a ser anunciados como `1`
  numa revisão anterior justamente por honestidade a essa limitação;
  agora reportam `max_tx_profiles` de verdade. `ChargePointMaxProfile`/
  `TxDefaultProfile` continuam sem stacking entre si (1 perfil "de
  fundo" de cada vez), mas um `TxProfile` sempre tem precedência sobre
  eles. `ClearChargingProfile` ganhou junto a limpeza seletiva por
  critério (`id`/`purpose`/`stackLevel`) que faltava — antes limpava
  tudo incondicionalmente, o que apagaria os outros perfis instalados
  por engano agora que existe mais de um simultâneo.
