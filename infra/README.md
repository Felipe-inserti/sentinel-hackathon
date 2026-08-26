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
| `orchestrator-sa` | Consumir domínio suspeito | `roles/pubsub.subscriber` | subscription `sub-orchestrator` | — |
| | Ler/gravar investigações e métricas | `roles/datastore.user` | projeto inteiro¹ | — |
| | Classificar com Gemini | `roles/aiplatform.user` | projeto inteiro | — |
| | Publicar investigação concluída | `roles/pubsub.publisher` | tópico `investigation-completed` | Publicar em `takedown-approved`, aprovar takedown |
| `evidence-sa` | Consumir investigação concluída | `roles/pubsub.subscriber` | subscription `sub-evidence` | Publicar em qualquer tópico Pub/Sub, chamar Vertex AI |
| | Gravar evidência (screenshot/HTML sanitizado) | `roles/storage.objectAdmin` | bucket `<project_id>-sentinel-evidence` | — |
| | Ler/gravar Firestore | `roles/datastore.user` | projeto inteiro¹ | — |
| `takedown-sa` | Consumir aprovação de takedown | `roles/pubsub.subscriber` | subscription `sub-takedown` | Publicar em qualquer tópico Pub/Sub (nunca publica nada) |
| | Reconfirmar aprovação, auditar, rate limit | `roles/datastore.user` | projeto inteiro¹ ² | — |
| | Decidir canais / redigir notificação com Gemini | `roles/aiplatform.user` | projeto inteiro | — |
| `dashboard-sa` | Ler/gravar investigações (decisão humana: `approved_by`/`decision_rationale`/etc.) e métricas | `roles/datastore.user` | projeto inteiro¹ | Chamar Vertex AI, escrever no bucket de evidência |
| | Ler artefatos de evidência (proxy de screenshot/HTML pro dashboard) | `roles/storage.objectViewer` | bucket `<project_id>-sentinel-evidence` | — |
| | Publicar aprovação de takedown | `roles/pubsub.publisher` | tópico `takedown-approved` | — |

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
