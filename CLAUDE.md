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

### Tópicos Pub/Sub existentes
`suspicious-domain-detected` · `investigation-completed` · `takedown-approved`

### Subscriptions existentes
`sub-orchestrator`

### Firestore
Coleção `investigations`

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