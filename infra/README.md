# Sentinel — Agent Identity (Terraform)

Provisiona uma **Service Account por agente**, com permissão mínima
(zero-trust) — o requisito de "Agent Identity" da trilha Fortified
Enterprise Fleet. Complementa o **Agent Registry** (`registry.py`,
`seed_registry.py`): o registry descreve o *contrato* de cada agente
(schema de entrada/saída, `tools_allowed`, `required_permissions`); este
Terraform é o que torna `required_permissions` real em IAM, não apenas
documentação.

## O que este Terraform gerencia (e o que não gerencia)

`scripts/setup_gcp.sh` (gcloud, já existente) continua sendo o dono dos
tópicos `suspicious-domain-detected`/`investigation-completed`, da
subscription `sub-orchestrator` e do banco Firestore — este Terraform só
os **referencia pelo nome** nos bindings de IAM abaixo, para não ter dois
donos do mesmo recurso.

Recursos novos, criados por este Terraform (nenhum script os criava
antes):

- Tópico `takedown-approved` e subscription `sub-takedown` — CLAUDE.md já
  descrevia `takedown-approved` como tópico "existente", mas nenhum
  script realmente o criava.
- Subscription `sub-evidence`, sobre o tópico `investigation-completed`
  já existente (mesmo tópico que `orchestrator-sa` publica) — nova em
  Sprint 4, quando `evidence-collector` passou a ter implementação real
  (`evidence_agent.py`); antes disso `evidence-sa` não tinha nenhuma
  permissão de Pub/Sub porque não havia nada rodando para consumir.
- Bucket do Cloud Storage para evidência (`evidence-sa`).
- As 5 Service Accounts e todos os bindings de IAM. Em Sprint 5
  (`dashboard/`), `dashboard-sa` passou de `roles/datastore.viewer`
  (só leitura) para `roles/datastore.user` (leitura/gravação) — a fila de
  revisão humana grava a decisão (`approved_by`/`decision_rationale`/etc.)
  no mesmo documento que lê — e ganhou `roles/storage.objectViewer` no
  bucket de evidência, pra servir screenshot/HTML sem expor `gs://` direto
  ao navegador. Em Sprint 6 (`takedown_agent.py`), `takedown-sa` ganhou
  `roles/datastore.user` e `roles/aiplatform.user` — deixou de ser o único
  binding do arquivo (ver seção "Por que `takedown-sa` é a peça central" e
  nota ² da matriz abaixo sobre por que isso não enfraquece a garantia
  central de segurança).

## Uso

```bash
cd infra
terraform init
terraform plan  -var="project_id=<SEU_PROJECT_ID>"
terraform apply -var="project_id=<SEU_PROJECT_ID>"
```

Pré-requisito igual ao de qualquer projeto Terraform+Google: uma credencial
válida (`gcloud auth application-default login`, ou uma service account
key via `GOOGLE_APPLICATION_CREDENTIALS`) com permissão de
Owner/Editor/IAM Admin no projeto alvo — o provider precisa disso mesmo só
para calcular o `plan`, mesmo que nenhum recurso ainda exista.

## Matriz de permissões

| Service Account | Pode fazer | Papel IAM | Escopo | Não pode fazer |
|---|---|---|---|---|
| `ct-listener-sa` | Publicar domínio suspeito | `roles/pubsub.publisher` | tópico `suspicious-domain-detected` | Ler/escrever Firestore, chamar Vertex AI, publicar em qualquer outro tópico |
| | Exportar traces/métricas (`telemetry.py`) ³ | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` | projeto inteiro¹ | — |
| `orchestrator-sa` | Consumir domínio suspeito | `roles/pubsub.subscriber` | subscription `sub-orchestrator` | — |
| | Ler/gravar investigações e métricas | `roles/datastore.user` | projeto inteiro¹ | — |
| | Classificar com Gemini | `roles/aiplatform.user` | projeto inteiro | — |
| | Publicar investigação concluída | `roles/pubsub.publisher` | tópico `investigation-completed` | Publicar em `takedown-approved`, aprovar takedown |
| | Exportar traces/métricas (`telemetry.py`) ³ | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` | projeto inteiro¹ | — |
| `evidence-sa` | Consumir investigação concluída | `roles/pubsub.subscriber` | subscription `sub-evidence` | Publicar em qualquer tópico Pub/Sub, chamar Vertex AI |
| | Gravar evidência (screenshot/HTML sanitizado) | `roles/storage.objectAdmin` | bucket `<project_id>-sentinel-evidence` | — |
| | Ler/gravar Firestore | `roles/datastore.user` | projeto inteiro¹ | — |
| | Exportar traces/métricas (`telemetry.py`) ³ | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` | projeto inteiro¹ | — |
| `takedown-sa` | Consumir aprovação de takedown | `roles/pubsub.subscriber` | subscription `sub-takedown` | Publicar em qualquer tópico Pub/Sub (nunca publica nada) |
| | Reconfirmar aprovação, auditar, rate limit | `roles/datastore.user` | projeto inteiro¹ ² | — |
| | Decidir canais / redigir notificação com Gemini | `roles/aiplatform.user` | projeto inteiro | — |
| | Exportar traces/métricas (`telemetry.py`) ³ | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` | projeto inteiro¹ | — |
| `dashboard-sa` | Ler/gravar investigações (decisão humana: `approved_by`/`decision_rationale`/etc.) e métricas | `roles/datastore.user` | projeto inteiro¹ | Chamar Vertex AI, escrever no bucket de evidência |
| | Ler artefatos de evidência (proxy de screenshot/HTML pro dashboard) | `roles/storage.objectViewer` | bucket `<project_id>-sentinel-evidence` | — |
| | Publicar aprovação de takedown | `roles/pubsub.publisher` | tópico `takedown-approved` | — |
| | Exportar traces/métricas ³ | `roles/cloudtrace.agent`, `roles/monitoring.metricWriter` | projeto inteiro¹ | — |
| `gateway-sa` (Sprint 8B) | Rotear invocação de `orchestrator`/`evidence-collector` | `roles/pubsub.publisher` | tópicos `suspicious-domain-detected` e `investigation-completed` | Publicar em `takedown-approved` (NUNCA — ver decisão abaixo), chamar Vertex AI |
| | Ler registry, gravar log de auditoria/rate limit do gateway | `roles/datastore.user` | projeto inteiro¹ | — |

³ Adicionado depois de uma sessão de validação de 48h (Sprint 8) que achou
as 5 SAs deste arquivo (`ct-listener-sa`/`orchestrator-sa`/`evidence-sa`/
`takedown-sa`/`dashboard-sa`) SEM `roles/cloudtrace.agent`/
`roles/monitoring.metricWriter` — `telemetry.py` estava funcionalmente
correto (inclusive a propagação de `trace_id` pelo Pub/Sub), mas toda
tentativa de exportar span/métrica falhava com `PERMISSION_DENIED` em
`telemetry.traces.write`/`monitoring.timeSeries.create`, silenciosamente
(ver `telemetry.py::_try_build_span_processor`/`_try_build_metric_reader`,
best-effort por design — loga e segue, nunca derruba o processo). Só 4
processos chamam `telemetry.setup()` hoje (`ct_listener.py`/
`orchestrator.py`/`evidence_agent.py`/`takedown_agent.py`) — `dashboard-sa`
(Next.js, sem instrumentação OTel neste sprint) não emite telemetria ainda,
mas ganhou o mesmo papel por consistência das 5 SAs deste arquivo (custo
zero: um papel de projeto sem chamada correspondente no código não gera
escrita nenhuma). `gateway-sa` fica de fora desta correção — `agent_gateway.py`
também não chama `telemetry.setup()` ainda, mesma lacuna, fora do escopo
pontual desta correção.

¹ `roles/datastore.user`/`roles/datastore.viewer` são papéis de **projeto**
— o Firestore nativo não oferece IAM por coleção/documento sem regras de
segurança customizadas adicionais (fora do escopo deste sprint). Isso
significa, por exemplo, que `evidence-sa` tecnicamente também consegue ler
a coleção `investigations`, não só a sua própria. Documentado aqui de
propósito para não vender um isolamento mais fino do que o que existe de
fato — se isso virar um requisito real, o próximo passo é regras do
Firestore (`firestore.rules`) ou mover dados sensíveis por coleção para
projetos GCP separados.

² `takedown-sa` é o caso mais sensível dessa limitação: `roles/datastore.user`
tecnicamente permite ESCREVER em `investigations`, não só em
`takedown_actions`/`takedown_rate_limits` (as únicas coleções que o agente
deveria gravar) nem só LER `investigations` (a única operação que deveria
fazer nessa coleção — ver regra de dupla checagem do CLAUDE.md). Como o
IAM não resolve isso, a restrição "`investigations` é somente leitura para
takedown-sa" é imposta em **código de aplicação**, não em infraestrutura:
`takedown_agent.py` só acessa essa coleção através de
`ReadOnlyCollectionAccess`, um wrapper que não expõe `set`/`update`/`add`/
`delete` — qualquer tentativa de escrita ali é um `AttributeError` em
tempo de execução, coberto por teste
(`tests/test_takedown_agent.py::test_read_only_collection_access_*`). Isso
é um risco conhecido e aceito (não uma garantia de infraestrutura), pelo
mesmo motivo da nota ¹: se isso virar requisito real, o próximo passo são
regras do Firestore ou separar o banco por projeto GCP.

## Por que `takedown-sa` é a peça central deste desenho

A regra inegociável do `CLAUDE.md` — "nenhum takedown sem aprovação humana
registrada" — não pode depender só do código lembrar de checar uma
aprovação antes de agir; código tem bug, é reescrito, é copiado errado.
Isso NÃO significa, porém, que `takedown-sa` seja incapaz por permissão de
fazer qualquer outra coisa: desde o Sprint 6 (`takedown_agent.py`) ela tem
`roles/datastore.user` (reconfirmar aprovação, gravar auditoria/rate
limit) e `roles/aiplatform.user` (Gemini decide canais/redige a
notificação) — capacidades reais, necessárias pelas próprias regras de
segurança que este agente implementa (dupla checagem no Firestore, seleção
de canal via modelo).

A garantia real aqui é **topológica**, não "zero permissão": `takedown-sa`
tem **um único binding de Pub/Sub** (`roles/pubsub.subscriber` na
subscription `sub-takedown`) e **nenhum `roles/pubsub.publisher` em
nenhum tópico** — ela não consegue publicar em `takedown-approved` nem em
mais nada. `sub-takedown` só recebe mensagens que alguém publicou nesse
tópico, e a única identidade com `roles/pubsub.publisher` ali é
`dashboard-sa` (ver bloco de IAM dela abaixo), que só publica depois de
gravar `approved_by`/`approved_at`/`decision_rationale` no Firestore via
um fluxo autenticado (Sign In With Google, ver `dashboard/README.md`).
Mesmo um `takedown-sa` totalmente comprometido não consegue **criar** uma
aprovação nem notificar sem que uma já exista — ele só age sobre o que
`dashboard-sa` publicou. O que ele PODE fazer de errado, se comprometido,
é mal-usar as permissões de Firestore/Vertex AI que tem — ex: escrever
lixo em `investigations` apesar do wrapper de aplicação (nota ² acima), ou
gastar cota de Vertex AI. Isso é um risco residual aceito, documentado,
não escondido atrás de uma alegação de isolamento total que deixou de ser
verdade.

## Decisão — o Agent Gateway (Sprint 8, Parte A) NUNCA ganha publish em `takedown-approved`

`agent_gateway.py` (Sprint 8, Parte A — fora deste Terraform, roda como
Cloud Run service, com SA própria a provisionar na Parte B) é o ponto
único de entrada HTTP para invocar qualquer agente do registry, e resolve
uma invocação publicando no tópico Pub/Sub que o agente-alvo consome. Dar
esse mesmo tratamento a `takedown-agent` foi cogitado e **rejeitado**
depois de revisão explícita: `agent_gateway.py::AUTHORIZATION_POLICY["takedown-agent"]`
é `frozenset()` — nenhum chamador, nem uma identidade equivalente a
`dashboard-sa`, consegue invocar `takedown-agent` pelo gateway. A rejeição
devolve um erro estruturado dedicado
(`human_approval_required_via_dashboard`, não o "não autorizado"
genérico) explicando que o único caminho é o fluxo humano do dashboard.

Por quê: dar à SA do gateway `roles/pubsub.publisher` em
`takedown-approved` — necessário para rotear qualquer coisa para lá —
criaria um **segundo publisher** nesse tópico. A defesa em profundidade de
`takedown_agent.py::_load_verified_approval` (reconfirma a aprovação no
Firestore antes de agir, independente de quem publicou a mensagem)
continuaria funcionando mesmo assim — mas a garantia mais forte e mais
fácil de auditar hoje, "um único publisher, o fluxo humano do dashboard",
vale mais que a conveniência de um segundo caminho síncrono para o mesmo
efeito. Por isso a matriz de IAM da Parte B NÃO deve incluir
`roles/pubsub.publisher` em `takedown-approved` para a SA do gateway —
`dashboard-sa` continua sendo a ÚNICA identidade com essa permissão,
exatamente como documentado na seção acima. A garantia topológica
original permanece intacta; o gateway não é "mais uma camada" dela, é um
caminho que foi conscientemente deixado de fora.

## Sprint 8, Parte B — Deploy (Cloud Run)

Arquivos novos: `apis.tf`, `artifact_registry.tf`, `cloud_run_gateway.tf`,
`cloud_run_jobs.tf`, `budget.tf`, mais variáveis/outputs adicionados
(nunca removidos) em `variables.tf`/`outputs.tf`. Nenhum recurso do
`main.tf` original foi alterado.

### Por que Jobs sob demanda, não Services, para os 4 workers

`ct_listener.py`/`orchestrator.py`/`evidence_agent.py`/`takedown_agent.py`
são todos processos "conecte e rode para sempre" — um websocket
(`certstream`) ou um `subscribe()` de streaming do Pub/Sub — **sem
nenhum servidor HTTP**. Cloud Run Service só escala (inclusive escalar a
zero de verdade) em resposta a requisições HTTP recebidas; sem um
endpoint para receber tráfego, um Service desses processos ficaria preso
em `min-instances=1` para sempre (ou nunca receberia tráfego nenhum) —
adicionar um endpoint `/healthz` só para satisfazer esse contrato seria
mudar código de aplicação fora do escopo "aditivo" deste sprint.

Cloud Run **Job** é o primitivo certo para um processo de longa duração
sem HTTP: você `execute` uma instância dele quando precisa, ela roda até
terminar/ser cancelada/bater o timeout, e — confirmado contra a
documentação oficial nesta sessão — **não há cobrança nenhuma entre
execuções**, só durante o tempo em que uma execução está de fato rodando.
Isso é o que torna "custo perto de zero fora da janela de demo" possível
sem tocar em nenhuma linha dos 4 agentes.

### Limitação honesta do `ct-listener-job`: NÃO é cobertura 24/7

Diferente de `orchestrator-job`/`evidence-collector-job`/
`takedown-agent-job` — que consomem de uma subscription Pub/Sub própria,
e o Pub/Sub **retém a mensagem** (`message_retention_duration=86400s`,
até 1 dia) enquanto nenhum worker está rodando para consumi-la, então
"job desligado" não perde nada, só atrasa o processamento —
`ct-listener-job` conecta a um **feed público efêmero de terceiros**
(`certstream.calidog.io`, via websocket). Não existe replay: um
certificado que passa pelo stream enquanto o job não está executando é
**perdido para sempre**, não fica esperando em fila em lugar nenhum.

Isso significa que este projeto, deployado como está, **não monitora
Certificate Transparency continuamente**. Ele captura domínios suspeitos
de verdade só durante as janelas em que você roda
`gcloud run jobs execute ct-listener-job` (demo, gravação, teste manual).
Fora dessas janelas, o pipeline de detecção fica parado — não é uma
simulação nem dado fake quando está rodando (o certstream é real, o
prefiltro e a triagem são reais), mas a cobertura no tempo é
intencionalmente parcial, por decisão de custo (ver Parte B do Sprint 8).

Cobertura contínua de verdade exigiria um worker sempre ativo (Cloud Run
Service com `min-instances=1` permanente, ou uma VM/GKE) — cogitado e
descartado para este sprint: custaria ~US$5-15/mês rodando 24/7 mesmo
sem tráfego nenhum de certificados maliciosos, e exigiria adicionar um
endpoint HTTP trivial a `ct_listener.py` só para caber no contrato de
Cloud Run Service (mudança de código de aplicação, fora do escopo deste
sprint sem aprovação explícita). Se isso virar um requisito real, o
próximo passo é exatamente esse: endpoint `/readyz` (**não** `/healthz` —
ver nota abaixo, seção "Testando o agent-gateway") + Service com
`min-instances=1`, revisado à parte.

### Uso — `deploy.sh` / `teardown.sh`

```bash
./deploy.sh <PROJECT_ID> [REGION]      # builda imagens, aplica Terraform (com confirmação do plan)
gcloud run jobs execute ct-listener-job --project <PROJECT_ID> --region <REGION> --async
# ... grave a demo ...
./teardown.sh <PROJECT_ID> [REGION]    # cancela execuções de Job ainda rodando, confirma min-instances=0
```

### Testando o agent-gateway

`GET /readyz` não exige autenticação (readiness do Cloud Run — **não**
`/healthz`: reproduzido em produção nesta sessão de validação, o Google
Frontend do Cloud Run intercepta esse path específico para o probe da
própria plataforma e a requisição nunca chega ao FastAPI, então qualquer
`curl`/monitoramento contra `/healthz` bate 404/comportamento do Frontend,
não do `agent_gateway.py`). Todo o resto exige um ID token do Google —
`agent_gateway.py::verify_google_id_token` usa a claim `email` do token
como identidade do chamador, então o token **precisa** ser emitido com
essa claim:

```bash
GATEWAY_URL="$(cd infra && terraform output -raw gateway_url)"

curl "${GATEWAY_URL}/readyz"

# --include-email e OBRIGATORIO -- sem ela o token do gcloud NAO carrega a
# claim "email", e verify_google_id_token rejeita com "ID token valido, mas
# sem claim 'email'" (401, etapa "authentication"). Sem essa flag e o erro
# mais comum ao reproduzir este projeto manualmente.
TOKEN="$(gcloud auth print-identity-token --include-email)"

curl -H "Authorization: Bearer ${TOKEN}" "${GATEWAY_URL}/agents"
curl -H "Authorization: Bearer ${TOKEN}" -X POST "${GATEWAY_URL}/invoke/orchestrator" \
  -d '{"...": "..."}'
```

Nem `deploy.sh` nem `teardown.sh` rodam `terraform destroy` — a
infraestrutura (Service Accounts, tópicos, Firestore, bucket, o próprio
Job/Service como *recurso*) fica no lugar entre uma demo e outra, porque
não custa nada parada. Só o que está ATIVO (execução de Job rodando,
instância de Service acima de zero) tem custo — é exatamente isso que
`teardown.sh` garante que volte a zero.

### Nota sobre reprodutibilidade — orçamento (`budget.tf`) pode exigir passo manual no Console

`terraform apply` do orçamento (`google_billing_budget.sentinel`) pode
falhar com **403** em `billingbudgets.googleapis.com`, citando um projeto
`consumer:` diferente do projeto alvo — a Billing Budgets API cobra a
chamada contra o *quota project* das credenciais ADC locais, não contra
`var.project_id` (ver comentário completo em `budget.tf`). `deploy.sh`
(etapa 1) já roda `gcloud auth application-default set-quota-project
<PROJECT_ID>` para corrigir isso automaticamente; rodando
`terraform apply` manualmente (sem passar por `deploy.sh`), é fácil
esquecer esse passo e bater no mesmo 403.

Isso resolve o caso mais comum, mas **não é garantido** — a Billing
Budgets API também exige que a identidade autenticada tenha um papel IAM
na própria *conta de faturamento* (`roles/billing.admin` ou
`roles/billing.user`), não no projeto; em algumas contas essa concessão só
pode ser feita por quem já é administrador da conta de faturamento, via
**Console** (`console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>` →
"Controle de acesso à conta de faturamento" → adicionar principal). Se
`set-quota-project` não resolver, esse é o próximo passo — documentado
aqui em vez de deixar como uma falha silenciosa de reprodutibilidade, já
que o regulamento do hackathon exige honestidade sobre o que reproduz de
verdade só com os scripts deste repositório.
