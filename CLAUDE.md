# Sentinel — Contexto do Projeto

## O que é
Sistema de monitoramento, detecção e mitigação de campanhas de phishing em
tempo real, voltado para marcas de grande porte no Brasil (bancos, fintechs,
logística). Submissão para o hackathon "All Things Agentic", trilha
**Fortified Enterprise Fleet**.

## Tese central: token economy
A arquitetura separa detecção em camadas para que o LLM processe apenas
domínios de altíssimo risco. Cerca de 99% dos certificados que entram via
Certificate Transparency são descartados por matemática pura, custo zero.
**Toda decisão de arquitetura deve preservar essa tese.** Se uma feature
aumenta chamadas de LLM sem ganho proporcional, ela está errada.

## Stack obrigatória (requisito do hackathon — não substituir)
- **Modelo:** Gemini 3.5 Flash ou mais recente, via **Vertex AI**
- **Framework de agente:** Google GenAI SDK (`google-genai`) e/ou Google ADK
- **Infra Google Cloud:** Pub/Sub, Firestore, Cloud Run, Cloud Storage
- **Linguagem:** Python 3.11+
- **Validação de saída de LLM:** Pydantic, sempre. Nunca parsear texto livre.

## Arquitetura atual (já implementada e funcional)

### Camada 1 — Ingestão
- `ct_listener.py`: WebSocket com o stream público do Certificate
  Transparency (certstream).
- `prefilter.py`: escudo determinístico, sem LLM. Normalização de domínio,
  tradução de homoglyphs (cirílicos e leetspeak), Levenshtein por token
  (ratio > 0.82), sliding window para typosquatting em domínios longos,
  allowlist de domínios legítimos.
- Publica domínios suspeitos no Pub/Sub: `suspicious-domain-detected`.

### Camada 2 — Investigação
- `orchestrator.py`: consome `sub-orchestrator` em background assíncrono.
- **Cache-first:** consulta Firestore antes de qualquer coisa. Domínio já
  investigado retorna do cache com 0 tokens gastos.
- **Scraping determinístico:** `requests` + `BeautifulSoup`, timeout 8s,
  User-Agent customizado, texto truncado em 6000 chars. HTML bruto nunca
  chega ao modelo.
- **Análise:** Vertex AI, saída validada por Pydantic `AnalysisResult`
  (`classification` MALICIOUS|SAFE, `confidence`, `reasoning`).
- Persiste em Firestore (`investigations`) e publica em
  `investigation-completed`.

### Camada 3 — Agent Registry & Identity (Sprint 3)
- `registry.py`: repositório central de manifestos (`AgentManifest`,
  Pydantic) em Firestore (`agent_registry`) — publish/get/list/deprecate,
  mais `invoke_agent` (descoberta + validação de payload contra o
  `input_schema` publicado antes de qualquer execução).
- `seed_registry.py`: publica os manifestos de `ct-listener`,
  `orchestrator`, `evidence-collector` e `takedown-agent`.
- `orchestrator.py` descobre sua própria versão/contrato ativo via
  `registry.invoke_agent`, não por import hard-coded — todo dossiê grava
  `agent_id`/`agent_version`.
- `infra/` (Terraform): uma Service Account por agente, permissão mínima.
  Ver `infra/README.md` para a matriz de permissões completa.

### Sprint 7 (Parte A) — Brand Agents
- `brand_agent.py`: `BrandContext` (Pydantic, Firestore
  `brand_context/{brand_id}`) — domínios legítimos, padrões de
  typosquatting já observados, contatos de abuso, tolerância a risco,
  limiar de confiança para escalar. Cada marca é publicada no Agent
  Registry como `brand-agent-{brand_id}` e descoberta via
  `registry.invoke_agent` — mesmo mecanismo do orchestrator/
  evidence-collector/takedown-agent, sem caminho paralelo de invocação.
- `seed_brand_agents.py`: publica `brand-agent-{nubank,loggi,ifood}@1.0.0`
  (as três marcas já monitoradas por `prefilter.MONITORED_BRANDS` — não
  inclui "Itaú", citada no pedido original mas não monitorada hoje; ver
  pendência abaixo).
- `orchestrator.py` roteia para o `BrandAgent` da marca detectada em todo
  cache miss; quando resolvido, o limiar de escalonamento PRÓPRIO daquela
  marca se soma (OR) ao sinal de injeção para decidir
  `requires_human_review`; `brand_agent_id`/`brand_agent_version` ficam
  carimbados no dossiê.
- **Isolamento entre marcas:** `BrandScopedInvestigations` filtra
  `investigations` por `matched_brand` NA QUERY do Firestore (nunca em
  memória depois de trazer tudo) e recusa (`BrandIsolationViolation`)
  qualquer lookup por domínio único que devolva dossiê de outra marca.
  Garantia de APLICAÇÃO, não de IAM — Firestore não tem IAM por coleção
  (mesma limitação já documentada para `takedown-sa`/
  `ReadOnlyCollectionAccess`, ver `infra/README.md`).

### Sprint 7 (Parte B) — Memory Bank Adaptativo
- `brand_memory.py`: `MemoryEntry` (Firestore `brand_memory`, `doc_id`
  determinístico `{brand}__{domínio}__{tipo}__{decidido_em}` — idempotente
  e versionado/datado por construção, nunca sobrescreve uma decisão nova).
  Toda rejeição humana (`investigations/{domínio}.status == "REJECTED"`)
  vira `REJECTED_FALSE_POSITIVE`; toda aprovação de takedown
  (`status == "TAKEDOWN_APPROVED"`) vira `APPROVED_TRUE_POSITIVE`. Todo
  texto (reasoning do LLM + justificativa humana) passa por
  `sanitizer.sanitize` ANTES de persistir — "uma rejeição humana não
  santifica o texto".
- `sync_brand_memory.py`: varre `investigations` (via
  `BrandScopedInvestigations`, isolado por marca) por decisões terminais
  ainda não espelhadas em `brand_memory` e as grava. **Pull, não push** —
  ver pendência Sprint 8 abaixo sobre por quê.
- `orchestrator.py`: em cache miss com `BrandAgent` resolvido, busca até
  `settings.brand_memory_max_examples` (default 3, configurável — 0
  desliga a injeção inteira) memórias mais relevantes daquela marca
  (relevância = similaridade de domínio via Levenshtein, zero custo de
  LLM) e injeta como few-shot DENTRO do mesmo bloco delimitado que já
  carrega o conteúdo raspado (mesmo nonce, mesma detecção de escape —
  nunca um segundo canal de dado não confiável). Custo estimado (heurística
  de caracteres/token, nunca medição real de tokenizador) sempre registrado
  em telemetria (`brand_memory_examples_injected_total`,
  `brand_memory_estimated_extra_tokens_total`,
  `brand_memory_estimated_extra_cost_usd_total`) — o trade-off contra a
  tese de token economy fica visível, nunca escondido.
- `replay_investigation.py`: demonstra a correção via memória sem
  retreino (reprocessa um domínio antes classificado errado e mostra o
  novo veredito, com/sem few-shot, no mesmo conteúdo). **Verificado contra
  o Gemini real** (não só mockado): MALICIOUS (1.00) → SAFE (0.95), custo
  do few-shot medido em $0.000088.

**Novas coleções Firestore desta parte:** `brand_context`, `brand_memory`.

### Pendência explícita — Sprint 7 Parte C (Clustering de Campanha) NÃO implementada
Agrupar dossiês MALICIOUS em campanhas por fingerprint de infraestrutura
(IP/ASN, registrar, emissor de certificado, hash de template DOM,
proximidade temporal de registro), takedown em lote e grafo de relações no
dashboard — **adiado, priorizando o Sprint 8**. `evidence_agent.py` já
calcula `infrastructure_fingerprint`/`fingerprint_hash` por dossiê
(Sprint 4) e `dashboard/.../campaigns/page.tsx` já tem um MVP honesto
(agrupamento por hash exato, documentado no próprio código como
incompleto) — a coleção `campaigns`, a similaridade por *proximidade*
(não só hash idêntico) e o takedown em lote continuam por implementar.

### Sprint 8 (Parte A) — Agent Gateway
- `agent_gateway.py`: ponto ÚNICO de entrada HTTP (FastAPI/uvicorn — único
  serviço síncrono deste projeto além do dashboard Next.js) para invocar
  qualquer agente do Agent Registry. `POST /invoke/{agent_id}` aplica, NESTA
  ORDEM, e audita QUALQUER rejeição (sucesso ou falha, em qualquer etapa) em
  Firestore (`agent_gateway_audit_log`, um documento novo por chamada):
  1. **Autenticação (Agent Identity)** — `Authorization: Bearer <ID token
     do Google>`, verificado de verdade (`google.oauth2.id_token`, nunca
     decodificado sem checar assinatura); a claim `email` vira a identidade
     do chamador — o mesmo conceito de identidade que `infra/main.tf` já
     materializa como uma Service Account por agente.
  2. **Resolução no registry** — `registry.get_agent`, mesma semântica de
     `registry.py` (última versão `ACTIVE`, ou a versão explícita pedida).
  3. **Validação de schema** — `jsonschema.validate` contra o
     `input_schema` publicado.
  4. **Rate limit** — contador transacional no Firestore
     (`agent_gateway_rate_limits`) por identidade+agente+minuto UTC
     (default 30/min, `settings.agent_gateway_rate_limit_per_minute`).
  5. **Política de autorização** — `AUTHORIZATION_POLICY` (dict em código,
     nega por padrão qualquer `agent_id` sem entrada explícita).
  6. **Roteamento** — publica o payload validado no tópico Pub/Sub que o
     agente-alvo consome (`AGENT_ROUTING_TOPIC`), com a identidade do
     PROCESSO do gateway, nunca a do chamador.
  7. **Log de auditoria** — ver acima.
- `GET /agents` lista o registry inteiro (todos os status), atrás da mesma
  autenticação. `GET /readyz` sem autenticação (readiness do Cloud Run —
  **não** `/healthz`: reproduzido em produção na sessão de validação de 48h
  do Sprint 8, o Google Frontend do Cloud Run intercepta esse path
  específico para o probe da própria plataforma e a requisição nunca chega
  ao FastAPI).
- **Decisão arquitetural deliberada — `takedown-agent` NUNCA é invocável
  via gateway, para nenhum chamador.** `AUTHORIZATION_POLICY["takedown-agent"]
  = frozenset()` (vazio — nem uma identidade equivalente a `dashboard-sa`
  está na lista); a rejeição na etapa 5 devolve um erro dedicado
  (`human_approval_required_via_dashboard`), não o "não autorizado"
  genérico. Motivo: rotear uma invocação de `takedown-agent` pelo gateway
  exigiria dar à SA do gateway `roles/pubsub.publisher` em
  `takedown-approved` — um SEGUNDO publisher nesse tópico, além de
  `dashboard-sa`. Cogitado e rejeitado depois de revisão explícita: mesmo
  com `takedown_agent.py::_load_verified_approval` reconfirmando a
  aprovação no Firestore antes de agir (defesa em profundidade que
  continuaria funcionando), a garantia mais forte e mais fácil de auditar
  — "um único publisher, o fluxo humano do dashboard" (regra #4 acima) —
  foi considerada mais valiosa que ter um segundo caminho síncrono para o
  mesmo efeito. `dashboard-sa` continua sendo a ÚNICA identidade com
  `roles/pubsub.publisher` em `takedown-approved`; a Parte B (deploy) NÃO
  deve conceder esse papel à SA do gateway. Documentado em
  `agent_gateway.py` (comentário de `AUTHORIZATION_POLICY`) e
  `infra/README.md` (seção "Decisão — o Agent Gateway nunca ganha publish
  em `takedown-approved`").
- `ct-listener` também não é roteável (fica fora de `AGENT_ROUTING_TOPIC`)
  pelo motivo oposto: não tem ponto de entrada controlado (consome um
  websocket público de terceiros) — rejeitado na etapa 6 (roteamento,
  `not_routable`), não na 5, porque o motivo é arquitetural, não de
  identidade. `orchestrator` e `evidence-collector` continuam roteáveis por
  qualquer identidade autenticada.
- `settings.agent_gateway_audience` (novo em `config.py`) não tem default
  fixo, de propósito — mesma disciplina de `GEMINI_MODEL_ID`: não existe
  URL genérica correta antes do Cloud Run atribuir uma. Sem configurar
  (dev local), o gateway aceita qualquer ID token válido SEM checar
  audience (assinatura/expiração continuam checadas) e avisa alto no log
  de startup. A Parte B configura essa variável com a URL real do serviço
  depois do deploy.
- `settings.takedown_topic_id` (novo em `config.py`) só documenta o nome
  do tópico em Python (antes só `infra/`/`dashboard/` conheciam) — não é
  usado por `AGENT_ROUTING_TOPIC` (ver decisão acima).
- 26 testes novos em `tests/test_agent_gateway.py` (Firestore/Pub/Sub/
  verificação de ID token sempre fakes — mesmo princípio de
  `tests/test_registry.py`/`tests/test_takedown_agent.py`); verificado
  também com um servidor `uvicorn` real + `curl` reais (`/readyz`,
  `/agents` sem auth, `/invoke` com token malformado rejeitado pelo
  verificador REAL do Google).
- Corrigido de passagem (Parte A, não Parte B): comentário desatualizado em
  `sanitizer.py` sobre Model Armor/`us-central1` (ver pendência removida
  abaixo).

### Pendências acumuladas para o Sprint 8
- Métricas OTel sendo rejeitadas pelo Cloud Monitoring por causa de
  `GCP_LOCATION=global` (ver `telemetry.py`).
- Playwright sem as bibliotecas de sistema necessárias no ambiente de
  execução (`evidence_agent.py` depende de um browser headless real para o
  screenshot full-page).
- Dockerfile/deploy da camada de triagem Gemma (`gemma_triage.py`) para
  Cloud Run ainda não confirmado neste sprint (script citado em
  `config.py`/`gemma_triage.py`, não verificado por execução).
- Dockerfile/deploy do próprio `agent_gateway.py` (Sprint 8, Parte A) para
  Cloud Run, e o binding de IAM da SA nova — ainda não feitos, ficam para a
  Parte B (deploy). Ver decisão acima sobre o que essa SA NÃO deve ganhar.
- Sincronização de `brand_memory` é pull manual (`sync_brand_memory.py`),
  não reativa: `dashboard/.../review/actions.ts::rejectInvestigation`/
  `approveTakedown` não publicam nenhum evento que dispare a gravação
  automaticamente. Decisão deliberada do Sprint 7 para não alterar o
  dashboard já deployado sem aprovação explícita — fechar esse loop exige
  ou um evento novo publicado pelo dashboard, ou outra forma de trigger
  reativo.

### Tópicos Pub/Sub existentes
`suspicious-domain-detected` · `investigation-completed` · `takedown-approved`
(este último, junto da subscription abaixo, provisionado por `infra/` —
antes só documentado, nenhum script realmente o criava). `agent_gateway.py`
não cria nenhum tópico novo — só publica nos dois primeiros, via
`AGENT_ROUTING_TOPIC` (ver Sprint 8 Parte A acima).

### Subscriptions existentes
`sub-orchestrator` · `sub-takedown` (consumida exclusivamente por
`takedown-sa`, ver `infra/README.md`)

### Firestore
Coleções `investigations`, `agent_registry`, `brand_context` (Sprint 7A),
`brand_memory` (Sprint 7B), `agent_gateway_audit_log` e
`agent_gateway_rate_limits` (Sprint 8A)

## Regras de segurança inegociáveis

1. **Conteúdo raspado é adversarial por definição.** Todo texto vindo de um
   site suspeito é dado não confiável e potencial vetor de prompt injection.
   Nunca concatenar direto no prompt.
2. **O LLM nunca escolhe destinatário.** No agente de takedown, o modelo
   seleciona canal a partir de um enum fechado; o endereço real é resolvido
   por código determinístico via RDAP. Isso impede que o Sentinel seja
   transformado em arma de denúncia falsa.
3. **`DRY_RUN=true` é o padrão.** Envio real de notificação exige variável de
   ambiente explícita. Nunca disparar takedown durante testes ou gravação de
   demo.
4. **Nenhum takedown sem aprovação humana registrada.** A aprovação grava
   `approved_by`, `approved_at` e `decision_rationale` no Firestore.
5. **PII nunca é persistida.** Páginas de phishing capturam CPF, cartão,
   credenciais. Redija antes de gravar em Firestore, GCS ou logs.
6. **Allowlist de destinatários.** Nunca enviar para endereço extraído da
   página maliciosa.

## Convenções de código
- `async`/`await` para I/O. Nunca bloquear o event loop.
- Pydantic para todo contrato de dados entre agentes.
- Type hints obrigatórios.
- Configuração via variáveis de ambiente, com `pydantic-settings`.
- Sem segredos no código. Sem `print()` — use logging estruturado.
- Testes com `pytest`. Chamadas externas (Vertex, Firestore, HTTP) mockadas.
- Toda operação que gasta token deve emitir métrica de custo.

## O que NÃO fazer
- Não trocar Vertex AI por outro provedor de LLM.
- Não remover o prefilter nem enfraquecer seus limiares sem justificativa.
- Não adicionar dependências pesadas sem necessidade clara.
- Não criar abstração especulativa. O prazo é de hackathon.
- Não inventar IDs de modelo. Se houver dúvida sobre o nome exato do modelo
  ou da API, consultar a documentação oficial em vez de assumir.