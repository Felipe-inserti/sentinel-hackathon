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

### Pendências acumuladas para o Sprint 8
- Métricas OTel sendo rejeitadas pelo Cloud Monitoring por causa de
  `GCP_LOCATION=global` (ver `telemetry.py`).
- Playwright sem as bibliotecas de sistema necessárias no ambiente de
  execução (`evidence_agent.py` depende de um browser headless real para o
  screenshot full-page).
- Dockerfile/deploy da camada de triagem Gemma (`gemma_triage.py`) para
  Cloud Run ainda não confirmado neste sprint (script citado em
  `config.py`/`gemma_triage.py`, não verificado por execução).
- Comentário desatualizado em `sanitizer.py` (pesquisa sobre Model Armor)
  afirmando que a região `us-central1` é "compatível com o default de
  `config.gcp_location`" — o default real hoje é `global`
  (`GCP_LOCATION=global`, ver `config.py`), não `us-central1`.
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
antes só documentado, nenhum script realmente o criava)

### Subscriptions existentes
`sub-orchestrator` · `sub-takedown` (consumida exclusivamente por
`takedown-sa`, ver `infra/README.md`)

### Firestore
Coleções `investigations`, `agent_registry`, `brand_context` (Sprint 7A),
`brand_memory` (Sprint 7B)

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