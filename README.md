# Sentinel

Monitoramento, detecção e mitigação de campanhas de phishing em tempo
real para marcas de grande porte no Brasil (bancos, fintechs, logística).
Submissão para o hackathon "All Things Agentic", trilha **Fortified
Enterprise Fleet**.

Certificados suspeitos entram pelo Certificate Transparency, ~99% são
descartados por matemática pura (custo zero de LLM) e só o que sobra vira
uma investigação com Gemini na Vertex AI, validada por Pydantic, com
takedown que exige aprovação humana registrada. Contexto completo
(arquitetura, tese de token economy, stack, regras de segurança, sprints
já implementados) está em [`CLAUDE.md`](CLAUDE.md) — este README não
repete esse conteúdo, é a porta de entrada para quem está vendo o projeto
pela primeira vez.

## Arquitetura em 3 camadas

```
CT stream/polling ──▶ prefilter (sem LLM) ──▶ Pub/Sub ──▶ orchestrator
  (plane1_ingestion)   Levenshtein/homoglyph   suspicious-  cache-first no
                        typosquatting, 0 custo  domain-      Firestore, só
                                                 detected     então Gemini
                                                                  │
                     dashboard (Next.js) ◀── investigations ◀────┘
                     fila de revisão humana      (Firestore)
                            │
                     aprovação registrada
                     (approved_by/approved_at/decision_rationale)
                            │
                            ▼
                     takedown-agent (RDAP resolve o destinatário,
                     LLM só escolhe canal por enum fechado, DRY_RUN=true
                     por padrão)
```

1. **Ingestão** (`plane1_ingestion/`) — stream/polling de Certificate
   Transparency + escudo determinístico (`prefilter.py`): normalização de
   domínio, homoglyphs, Levenshtein, allowlist. Zero chamadas de LLM
   nesta camada.
2. **Investigação** (`plane2_agents/orchestrator.py`, `evidence_agent.py`)
   — cache-first no Firestore, scraping determinístico, captura de tela
   (multimodal), memória adaptativa por marca (few-shot), classificação
   via Gemini/Vertex AI com saída validada por Pydantic.
3. **Resposta** (`takedown_agent.py`, `dashboard/`) — fila de revisão
   humana, aprovação registrada, takedown com destinatário resolvido por
   código (nunca pelo LLM), `DRY_RUN=true` por padrão.

Mais: **Agent Registry & Identity** (`registry.py`, `infra/` — uma Service
Account por agente), **Agent Gateway** (`agent_gateway.py` — ponto único
de entrada HTTP autenticado/auditado para invocar agentes) e **Brand
Agents** (`brand_agent.py` — contexto e limiar de risco por marca,
isolamento garantido na query do Firestore). Detalhe de cada sprint em
[`CLAUDE.md`](CLAUDE.md#arquitetura-atual-já-implementada-e-funcional).

## Mapa: requisito da trilha → arquivo

| Requisito | Implementação | Arquivo |
|---|---|---|
| Agent Registry | Manifestos versionados (`AgentManifest`) em Firestore (`agent_registry`) | `registry.py`, `seed_registry.py` |
| Agent Runtime | 4 Cloud Run Jobs sob demanda (ct-listener, orchestrator, evidence-collector, takedown-agent) | `infra/cloud_run_jobs.tf` |
| Memory Bank | Memória adaptativa por marca, injetada como few-shot, custo sempre medido | `brand_memory.py`, `sync_brand_memory.py` |
| Agent Identity | 5 Service Accounts (ct-listener, orchestrator, evidence, takedown, dashboard), permissão mínima | `infra/main.tf` |
| Agent Gateway | Auth (ID token) + schema + rate limit + policy fechada + auditoria, ponto único de entrada HTTP | `agent_gateway.py` |
| Sanitização adversarial | Conteúdo raspado nunca concatenado direto no prompt; injeção detectada vira sinal de suspeita | `sanitizer.py` |
| Agent Observability | OpenTelemetry → Cloud Trace + Cloud Monitoring, span por etapa do pipeline | `telemetry.py` |

## Stack

Python 3.11+ · Gemini via Vertex AI (`google-genai`) · Pydantic para toda
saída de LLM · Pub/Sub, Firestore, Cloud Run (Jobs + Services), Cloud
Storage, Cloud Scheduler · Terraform para IAM (`infra/`) · Next.js 16 +
TypeScript para o dashboard (`dashboard/`) · pytest (chamadas externas
sempre mockadas).

## Estrutura do repositório

| Caminho | O que é |
|---|---|
| `plane1_ingestion/` | CT listener/polling, prefilter determinístico |
| `plane2_agents/` | Orchestrator, page_capture (multimodal) |
| `evidence_agent.py`, `takedown_agent.py` | Coleta de evidência e resposta |
| `registry.py`, `agent_gateway.py` | Agent Registry e gateway HTTP autenticado |
| `brand_agent.py`, `brand_memory.py` | Contexto e memória adaptativa por marca |
| `dashboard/` | Next.js — fila de revisão humana, custo, campanhas |
| `infra/` | Terraform — Service Account por agente, permissão mínima |
| `demo/` | Páginas de phishing simuladas + alvo de demo (Cloud Run) |
| `docs/` | Comandos de demo, red team, diagnóstico, relatórios |
| `tests/` | pytest — chamadas externas sempre mockadas |
| `FINDINGS.md` | Log cronológico de achados operacionais (verificados por execução) |

## Rodando localmente

Pré-requisitos: Python 3.11+, conta GCP com billing ativo,
[`gcloud` CLI](https://cloud.google.com/sdk/docs/install) autenticado
(`gcloud auth application-default login`). Ollama com Gemma é opcional —
sem ele o triage em lote faz *fail-open* (todo domínio vira `INVESTIGATE`,
comportamento verificado, ver tabela abaixo).

```bash
git clone https://github.com/Felipe-inserti/sentinel-hackathon
cd sentinel-hackathon

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium   # necessário para captura de tela (evidence_agent.py, plane2_agents/page_capture.py)

cp .env.example .env
# preencha pelo menos: GCP_PROJECT_ID, GEMINI_MODEL_ID, GCP_LOCATION

.venv/bin/python -m pytest -q
# verificado nesta sessão: 358 passed, 3 deselected
```

Demo local ponta a ponta (sobe um alvo de phishing simulado e roda o
fluxo completo sem depender do dashboard):

```bash
# sobe a variante "malicious" (BancoTeste, formulário de credencial) na porta 8000
./demo/phishing-target/serve.sh malicious 8000
echo "127.0.0.1 bancoteste-fake.sentinel.local" | sudo tee -a /etc/hosts

# roda investigação + aprovação simulada, sem precisar do dashboard nem do e-mail de demo
DEMO_INSECURE_HTTP=true DEMO_LOCAL_HTTP_PORT=8000 \
python demo_run_multimodal_flow.py bancoteste-fake.sentinel.local --brand bancoteste --simulate-approval
```

Roteiro completo de demo (incluindo variantes de injeção de prompt, e
como acionar um takedown real via SMTP) em
[`docs/DEMO_COMMANDS.md`](docs/DEMO_COMMANDS.md).

## Deploy na Google Cloud

```bash
# 1. APIs necessárias (idempotente — pula o que já existe)
./scripts/setup_gcp.sh   # habilita pubsub, firestore, aiplatform, telemetry; cria tópicos/subscriptions base

# 2. Infraestrutura (Service Accounts, IAM, tópicos/subscriptions restantes, buckets)
cd infra
terraform init
terraform plan  -var="project_id=<seu-projeto>" -var-file=<seu.tfvars>
terraform apply -var="project_id=<seu-projeto>" -var-file=<seu.tfvars>
cd ..

# 3. Build + push das 3 imagens (ct-listener+takedown-agent+agent-gateway,
#    evidence-collector, orchestrator) e deploy dos Cloud Run Jobs/Services
./deploy.sh
```

**Duas armadilhas documentadas** (ver [`infra/README.md`](infra/README.md)
e `FINDINGS.md` achados #15/#22):

1. As imagens usam tag mutável (`:latest`) — Terraform compara *strings*,
   não digests, então um push novo não gera diff sozinho. É preciso
   `terraform apply -replace=google_cloud_run_v2_job.<worker>` para forçar
   a re-resolução da tag.
2. Todo `-replace` num Cloud Run Job **derruba o IAM binding do
   Scheduler** sem sinalizar (o plano é calculado antes da destruição,
   antes que o Job novo exista). Sempre rode um segundo
   `terraform apply -target=google_cloud_run_v2_job_iam_member.scheduler_invoke_<worker>`
   e confirme com `gcloud run jobs get-iam-policy <worker>-job --region=us-central1`.

Verificação pós-deploy (trace de uma investigação real e métricas de
custo) em [`docs/DEMO_COMMANDS.md` §11](docs/DEMO_COMMANDS.md). Teardown
completo:

```bash
./teardown.sh
```

## Segurança

Regras inegociáveis (detalhe em [`CLAUDE.md`](CLAUDE.md#regras-de-segurança-inegociáveis)):
conteúdo raspado é sempre tratado como adversarial e nunca concatenado
direto no prompt; o LLM nunca escolhe destinatário de takedown (só um
canal de um enum fechado — o endereço real vem de RDAP, via código);
`DRY_RUN=true` por padrão; nenhum takedown sem aprovação humana
registrada; PII nunca persistida; allowlist de destinatários. Prova
adversarial completa (injeção de prompt tentando redirecionar takedown,
7 cenários) em [`docs/RED_TEAM.md`](docs/RED_TEAM.md).

## Limitações conhecidas

- **Firestore não tem IAM por coleção.** `roles/datastore.user`/`viewer`
  são papéis de projeto — `evidence-sa`, por exemplo, tecnicamente também
  consegue ler `investigations`, não só o que produz. O isolamento entre
  marcas (`BrandScopedInvestigations`) e a política de leitura de
  `takedown-sa` são garantias de **aplicação**, não de infraestrutura. Ver
  `infra/README.md`.
- **`ct-listener`** consome um websocket/polling público de terceiro
  (Certificate Transparency) — não tem ponto de entrada controlado, por
  isso é o único agente que fica fora do Agent Gateway (`not_routable`,
  decisão arquitetural, não falta de identidade).
- Clustering de campanha (Sprint 7C), sincronização reativa do
  `brand_memory` e deploy do Agent Gateway/Gemma triage para Cloud Run
  ainda não implementados — ver "Status" abaixo e `CLAUDE.md` para a lista
  completa de pendências.

## Status — o que está pronto e o que não está

**Implementado e com teste/execução real por trás:** as 3 camadas acima,
Agent Registry/Identity, Brand Agents com isolamento por marca, Memory
Bank adaptativo (verificado contra o Gemini real, não só mockado), Agent
Gateway com autenticação/rate limit/auditoria, migração de CT para
polling RFC 6962, classificação multimodal (captura de tela).

**Pendente, documentado explicitamente (não escondido):**
- Clustering de campanha (Sprint 7C) — `evidence_agent.py` já calcula o
  fingerprint de infraestrutura, mas agrupamento por proximidade, a
  coleção `campaigns` e takedown em lote ainda não existem.
- Deploy do `agent_gateway.py` e do Gemma triage (`gemma_triage.py`) para
  Cloud Run, e o binding de IAM da SA do gateway.
- `brand_memory` é sincronizado por pull manual (`sync_brand_memory.py`),
  não reativo a ações do dashboard.
- Playwright sem as bibliotecas de sistema no ambiente de execução atual
  (fora do Docker — dentro da imagem, `playwright install --with-deps
  chromium` já resolve isso, ver `Dockerfile.orchestrator`/`Dockerfile.evidence`).
- Métricas OTel rejeitadas pelo Cloud Monitoring por causa de
  `GCP_LOCATION=global` (ver `telemetry.py`).

Log cronológico completo de achados operacionais (o que quebrou, como foi
diagnosticado, o que foi corrigido e o que ficou pendente por decisão
explícita) em [`FINDINGS.md`](FINDINGS.md).

## O que acontece quando quebra

Nenhum destes é hipotético — cada linha aponta para o teste, o log de uma
execução real, ou o commit que prova o comportamento. "Verde na suíte" não
é a mesma coisa que "provado em produção" (ver último item desta tabela).

| Quando isso falha... | ...o sistema faz isso | Prova |
|---|---|---|
| **Gemma (triagem em lote) fica indisponível** | Fail-open: todo domínio do lote vira `INVESTIGATE`, nenhum é descartado silenciosamente | `tests/test_ct_listener_triage_integration.py::test_fail_open_when_gemma_service_is_down`, `tests/test_gemma_triage.py`. **Provado em produção**, não só em teste: run real de 31min desta sprint com Ollama fora do ar — `gemma_fallback_total = gemma_triage_total` (100% fail-open), pipeline seguiu até o Gemini normalmente |
| **certstream (websocket de CT de terceiro) fica fora do ar** | Migrado para polling direto do log RFC 6962 (Argon2026h2) com cursor persistido — uma queda vira atraso temporário, nunca perda permanente (era limitação honesta documentada antes desta sprint) | `plane1_ingestion/ct_rfc6962.py`, `tests/test_ct_rfc6962.py`, `tests/test_ct_listener_polling.py`; histórico da migração no `CLAUDE.md` e no git log |
| **Rate limit não documentado do log Argon2026h2** | Concorrência de ingestão sobe gradual e recua pela metade no primeiro 429 — nunca fica travada tentando de novo na mesma velocidade | `tests/test_ct_listener_parallel_ingestion.py`. **Provado ao vivo**: duas medições reais desta sprint bateram 429 de verdade (3 eventos cada); concorrência observada oscilando 4→2→1→2 numa corrida e 1→1→1→2→3→4→5 na outra, nunca travou |
| **Site alvo (evidência) fica fora do ar durante a coleta** | `EvidenceBundle` parcial (`is_partial=True`) — cada etapa que falhou (DNS/TLS/screenshot/RDAP) fica registrada, nunca esconde a lacuna fingindo bundle completo | `evidence_agent.py` (docstring do módulo + campo `is_partial`) |
| **Captura de tela falha** | Segue só com análise textual (`visual_analysis_available=False`), nunca bloqueia a investigação | `plane2_agents/page_capture.py`, `plane2_agents/orchestrator.py` |
| **RDAP devolve contato de abuso envenenado/mal-formado** | Rejeitado — só um único e-mail/URL bem formado é aceito; qualquer coisa com vírgula/ponto-e-vírgula/espaço é tratada como não-resolvível, nunca usada parcialmente | `takedown_agent.py::_is_single_valid_contact`; achado completo em [`docs/RED_TEAM.md`](docs/RED_TEAM.md) (Achado 1) |
| **LLM devolve JSON fora do schema Pydantic** | Retry com backoff no erro transitório; esgotadas as tentativas, falha de forma auditável (`LLMSchemaValidationError`), nunca aceita um formato parcial/inventado | `llm_client.py::LLMSchemaValidationError`, `_call_with_transient_retry` |
| **Pub/Sub reentrega a mesma mensagem (at-least-once)** | Cache-first no Firestore: a segunda entrega do mesmo domínio bate no cache (0 tokens gastos) em vez de reinvestigar | `plane2_agents/orchestrator.py::investigate_domain` (checagem `cache.lookup` antes de qualquer chamada de LLM) |
| **Conteúdo raspado tenta injeção de prompt** | Sanitizado antes do prompt (regex + remoção de caracteres invisíveis Unicode `Cf`); se a injeção for detectada E o veredito do modelo for `SAFE`, isso **vira sinal de suspeita**, não é só neutralizado — força revisão humana obrigatória (um site legítimo não tenta injetar o classificador) | `sanitizer.py`; `plane2_agents/orchestrator.py` (`requires_human_review = injection_patterns_found and classification == "SAFE"`); prova adversarial completa em [`docs/RED_TEAM.md`](docs/RED_TEAM.md) |
| **Teto de gasto externo do Vertex AI é atingido** | Chamada ao Gemini falha com `403 PERMISSION_DENIED` (`Spend cap breached`) — comportamento observado, não simulado; é um teto do GCP, não do Sentinel, então nenhum código do projeto o contorna ou esconde | Observado ao vivo nesta sprint (`cost_measurement*/orchestrator.log`, run de 27/08/2026) — não é um teste automatizado, é um log real de produção |
| **`terraform apply -replace` recria um Cloud Run Job** | O binding de IAM do Scheduler não sobrevive à recriação — exige um segundo `apply` isolado, alvo só no `_iam_member`, para restaurar | `infra/README.md` (seção "Regra operacional"); `FINDINGS.md` achado #22 |

### O padrão que mais importa desta lista

Suíte verde não prova produção. O exemplo mais caro desta sprint: 264
testes passavam com `observation_run.py` **ausente da imagem Docker** — o
módulo existia no repositório, tinha cobertura de teste, e mesmo assim
nunca chegou aos workers, porque o `Dockerfile` usa lista explícita de
arquivos (não `COPY . .`) e ninguém tinha adicionado a linha nova. Os
workers em produção crasharam ao importar um módulo que "existia". A
correção não foi só adicionar a linha — foi passar a confirmar, por
execução (simular a imagem/checar os arquivos copiados), toda vez que um
módulo novo é criado, nunca deduzir do `Dockerfile` que ele vai entrar.

Achado mais recente do mesmo padrão: a imagem `sentinel-orchestrator:latest`
em produção ficou 5 horas defasada do commit que corrigia dois bugs
(`FINDINGS.md` achado #19) — suíte local verde o tempo todo, produção
rodando código antigo. Diagnóstico completo, causa raiz e o que ainda
falta re-deployar em `FINDINGS.md` (achados #19-#22).

## Documentação

- [`CLAUDE.md`](CLAUDE.md) — arquitetura completa, tese de token economy, sprints, regras de segurança
- [`FINDINGS.md`](FINDINGS.md) — log cronológico de achados operacionais
- [`docs/DEMO_COMMANDS.md`](docs/DEMO_COMMANDS.md) — roteiro de demo, comando a comando
- [`docs/RED_TEAM.md`](docs/RED_TEAM.md) — prova adversarial (injeção de prompt não redireciona takedown)
- [`docs/PIPELINE_SWIMLANES.md`](docs/PIPELINE_SWIMLANES.md) — pipeline com custo real medido
- [`docs/EVIDENCE_VISION_DIAGNOSIS.md`](docs/EVIDENCE_VISION_DIAGNOSIS.md), [`docs/DETERMINISTIC_SIGNALS_PLAN.md`](docs/DETERMINISTIC_SIGNALS_PLAN.md) — diagnósticos e planos não implementados
- [`infra/README.md`](infra/README.md) — Terraform, matriz de permissões por agente
- [`dashboard/README.md`](dashboard/README.md) — decisões de arquitetura do dashboard
