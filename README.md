# Sentinel

Monitoramento, detecção e mitigação de campanhas de phishing em tempo
real para marcas de grande porte no Brasil. Submissão para o hackathon
"All Things Agentic", trilha Fortified Enterprise Fleet.

Contexto completo do projeto (arquitetura, tese de token economy, stack,
regras de segurança, sprints já implementados) está em
[`CLAUDE.md`](CLAUDE.md) — este README não repete esse conteúdo, só
documenta o comportamento do sistema sob falha.

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
| **RDAP devolve contato de abuso envenenado/mal-formado** | Rejeitado — só um único e-mail/URL bem formado é aceito; qualquer coisa com vírgula/ponto-e-vírgula/espaço é tratada como não-resolvível, nunca usada parcialmente | `takedown_agent.py::_is_single_valid_contact`; achado completo em [`docs/RED_TEAM.md`](docs/RED_TEAM.md) (Achado 1) |
| **LLM devolve JSON fora do schema Pydantic** | Retry com backoff no erro transitório; esgotadas as tentativas, falha de forma auditável (`LLMSchemaValidationError`), nunca aceita um formato parcial/inventado | `llm_client.py::LLMSchemaValidationError`, `_call_with_transient_retry` |
| **Pub/Sub reentrega a mesma mensagem (at-least-once)** | Cache-first no Firestore: a segunda entrega do mesmo domínio bate no cache (0 tokens gastos) em vez de reinvestigar | `plane2_agents/orchestrator.py::investigate_domain` (checagem `cache.lookup` antes de qualquer chamada de LLM) |
| **Conteúdo raspado tenta injeção de prompt** | Sanitizado antes do prompt (regex + remoção de caracteres invisíveis Unicode `Cf`); se a injeção for detectada E o veredito do modelo for `SAFE`, isso **vira sinal de suspeita**, não é só neutralizado — força revisão humana obrigatória (um site legítimo não tenta injetar o classificador) | `sanitizer.py`; `plane2_agents/orchestrator.py` (`requires_human_review = injection_patterns_found and classification == "SAFE"`); prova adversarial completa em [`docs/RED_TEAM.md`](docs/RED_TEAM.md) |
| **Teto de gasto externo do Vertex AI é atingido** | Chamada ao Gemini falha com `403 PERMISSION_DENIED` (`Spend cap breached`) — comportamento observado, não simulado; é um teto do GCP, não do Sentinel, então nenhum código do projeto o contorna ou esconde | Observado ao vivo nesta sprint (`cost_measurement*/orchestrator.log`, run de 27/08/2026) — não é um teste automatizado, é um log real de produção |

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
