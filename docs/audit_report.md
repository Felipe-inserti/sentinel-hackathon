# Auditoria de estado real — Sentinel

Gerado em 2026-08-26, executando comandos reais neste ambiente (mesmo
`.venv`, mesmo checkout, credenciais GCP reais via ADC do projeto
`seu-id-unico`). Cada afirmação abaixo é seguida do comando que a prova,
ou marcada **NÃO VERIFICADO** com o motivo. Nada aqui é "lembrado" de
sessões anteriores sem reconfirmação.

---

## 1. Integridade do código

### Arquivos .py na raiz e em `tests/`, com contagem de linhas

```
$ wc -l *.py
```
```
     3 conftest.py
    57 takedown.py
   133 config.py
   201 test_llm_client.py      <- ATENCAO: fica na RAIZ, nao em tests/
   207 llm_client.py
   246 metrics_report.py
   254 seed_registry.py
   280 gemma_triage.py
   282 registry.py
   305 sanitizer.py
   318 eval_triage.py
   329 telemetry.py
   789 evidence_agent.py
   962 takedown_agent.py
  4366 total
```

```
$ wc -l tests/*.py
```
```
   122 tests/test_orchestrator_registry.py
   171 tests/test_ct_listener_triage_integration.py
   203 tests/test_gemma_triage.py
   264 tests/test_registry.py
   284 tests/test_sanitizer.py
   389 tests/test_evidence_agent.py
   686 tests/test_takedown_agent.py
   709 tests/test_injection_cannot_redirect.py
  2828 total
```

Mais `plane1_ingestion/` (707 linhas: `ct_listener.py` 433 + `prefilter.py`
274) e `plane2_agents/orchestrator.py` (467 linhas).

**Achado — `prefilter.py` (274 linhas, o "escudo de custo zero" central da
tese do projeto) não tem NENHUM arquivo de teste dedicado:**
```
$ find tests -iname "*prefilter*"
(vazio)
```
`tests/test_ct_listener_triage_integration.py` não referencia
`analyze_domain`/`is_suspicious` nenhuma vez — cobre só a integração com a
triagem Gemma (fail-open), não a lógica de Levenshtein/homoglyph/allowlist
em si.

**Achado — `orchestrator.py` (467 linhas) tem só 1 teste dedicado fora do
registry:**
```
$ grep -n "^def test_" tests/test_orchestrator_registry.py
107:def test_save_investigation_stamps_agent_id_and_version(monkeypatch):
```
Não há teste cobrindo `scrape_website`, `classify_domain_with_gemini`
(cache-first, sanitização, chamada ao LLM) diretamente.

### `python -m pytest --collect-only -q`, total real, por arquivo

```
$ python3 -m pytest --collect-only -q
...
131/134 tests collected (3 deselected) in 12.00s
```

Por arquivo (contagem manual da listagem):

| Arquivo | Testes |
|---|---|
| `test_llm_client.py` (raiz) | 9 |
| `tests/test_ct_listener_triage_integration.py` | 3 |
| `tests/test_evidence_agent.py` | 18 |
| `tests/test_gemma_triage.py` | 7 |
| `tests/test_injection_cannot_redirect.py` | 7 (+3 `live_llm`, deselecionados por padrão) |
| `tests/test_orchestrator_registry.py` | 4 |
| `tests/test_registry.py` | 17 |
| `tests/test_sanitizer.py` | 23 |
| `tests/test_takedown_agent.py` | 43 |
| **Total coletado (padrão)** | **131** |

Sem erro de coleta (nenhum `ImportError`/`ERROR` na saída de
`--collect-only`) — confirmado grepando a saída por `error`/`ERROR`: só
apareceram nomes de função contendo a palavra "error", não falhas reais.

### Investigação da discrepância (b): "131" (meu último relatório) vs "42" (seu ambiente)

**Causa raiz confirmada, não um problema de ambiente seu:**

```
$ git log --oneline --all
424821f Initial commit — Sentinel: detecção e mitigação de phishing em tempo real

$ git show --stat HEAD
 .env.example ... config.py ... conftest.py ... eval_triage.py ...
 gemma_triage.py ... llm_client.py ... metrics_report.py ...
 plane1_ingestion/* ... plane2_agents/orchestrator.py ...
 requirements.txt ... sanitizer.py ... scripts/setup_gcp.sh ...
 takedown.py ... telemetry.py ... test_llm_client.py ...
 tests/test_ct_listener_triage_integration.py ...
 tests/test_gemma_triage.py ... tests/test_sanitizer.py
 24 files changed, 4251 insertions(+)
```

O commit único do repositório contém **exatamente** os arquivos dos
Sprints 0–2.5. Somando os testes só desses arquivos:

```
test_llm_client.py (9) + test_ct_listener_triage_integration.py (3)
+ test_gemma_triage.py (7) + test_sanitizer.py (23) = 42
```

**Isso bate exatamente com o número que você reportou.** Ou seja: se você
rodou `pytest` a partir de um checkout que só reflete o que está no git
(um `git clone` fresco, ou um diretório sincronizado só por `git pull`),
você literalmente não tem, no disco, nenhum arquivo dos Sprints 3–6:
`registry.py`, `seed_registry.py`, `evidence_agent.py`, `takedown_agent.py`,
`dashboard/`, `infra/`, `pytest.ini`, nem as modificações em `config.py`/
`telemetry.py`/`orchestrator.py`/`requirements.txt`/`.env.example` — **nada
disso foi commitado, em nenhuma sessão anterior.**

```
$ git status
On branch master
Your branch is up to date with 'origin/master'.

Changes not staged for commit:
	modified:   .env.example
	modified:   .gitignore
	modified:   CLAUDE.md
	modified:   FINDINGS.md
	modified:   config.py
	modified:   plane2_agents/orchestrator.py
	modified:   requirements.txt
	modified:   telemetry.py

Untracked files:
	dashboard/
	docs/
	evidence_agent.py
	infra/
	pytest.ini
	registry.py
	seed_registry.py
	takedown_agent.py
	tests/test_evidence_agent.py
	tests/test_injection_cannot_redirect.py
	tests/test_orchestrator_registry.py
	tests/test_registry.py
	tests/test_takedown_agent.py

$ git remote -v
origin	https://github.com/Felipe-inserti/sentinel-hackathon.git (fetch/push)

$ git branch -vv
* master 424821f [origin/master] Initial commit...
```

`origin/master` também está no commit inicial — **isso significa que o
GitHub também só tem os Sprints 0–2.5.** Não é filtragem do `pytest.ini`
(que só exclui `live_llm`, 3 testes) nem coleta silenciosa falhando — é
trabalho real, funcional, testado, que nunca saiu do working tree deste
ambiente.

**Isto é o achado mais grave desta auditoria — ver seção "Riscos".**

---

## 2. Dependências

### `requirements.txt` vs `pip freeze`

```
$ pip freeze | sort
```
(saída completa nos comandos rodados — 80 pacotes instalados no total)

**Faltando no ambiente instalado (declarados em `requirements.txt`, ausentes em `pip freeze`):**

| Pacote | Efeito |
|---|---|
| `opentelemetry-exporter-otlp-proto-grpc` | `OTLPSpanExporter`/`OTLPMetricExporter` não existem — `telemetry.py::_try_build_span_processor`/`_try_build_metric_reader` caem no `except Exception`, logam e devolvem `None` |
| `opentelemetry-resourcedetector-gcp` | `GoogleCloudResourceDetector` não existe — `telemetry.setup()` cai no `except Exception` e loga "Deteccao de recurso GCP falhou" (reproduzido ao vivo nesta sessão, ver seção 5) |

**Confirma (c) diretamente: os exportadores OTel NÃO estão instalados —
spans/contadores são criados normalmente (API do OTel funciona sem
processor/reader anexado), mas nada é exportado para Cloud
Trace/Monitoring.** Isso é consistente com o próprio código de
`telemetry.py`, que já trata essa falha como não-fatal — mas
funcionalmente, hoje, **nenhum trace chega ao Cloud Trace real** neste
ambiente.

**Usado no código mas ausente de `requirements.txt` (funciona por acidente, via dependência transitiva de outra coisa):**

| Pacote | Onde é importado | Risco |
|---|---|---|
| `httpx` | `gemma_triage.py` | Instalado agora (`0.28.1`), mas não declarado — um `pip install -r requirements.txt` limpo (ex: build de container do Cloud Run) **não o instalaria**, e `gemma_triage.py` quebraria com `ModuleNotFoundError` no import |

**Instalado mas não declarado nem usado por nenhum import do projeto**
(não é risco de quebra, mas indica que o ambiente real diverge do que
`requirements.txt` documenta): `google-cloud-aiplatform`,
`google-cloud-bigquery`, `google-cloud-resource-manager`, toda a família
`genkit`/`genkit-google-genai`/`genkit-plugin-google-genai`/
`dotpromptz`/`dotpromptz-handlebars`, `starlette`, `uvicorn`, `uvloop`,
`sse-starlette`, entre outros — confirmado via `grep -rn "aiplatform\|genkit\|dotpromptz"`
que nenhum desses é importado por nenhum `.py` do projeto (só
`"roles/aiplatform.user"`, uma string de nome de papel IAM, não o pacote
Python).

### `requirements.txt` tem versões fixadas?

```
$ grep -E "==" requirements.txt
(vazio)
```

**Não. Nenhuma linha tem `==versão`.** Todo `pip install -r requirements.txt`
resolve para "o que for mais novo disponível hoje" — isso é um risco real
para o Sprint 8 (deploy): uma imagem de container buildada hoje e outra
buildada na semana da avaliação podem instalar versões diferentes de
`google-genai`/`opentelemetry-*`/`playwright`/etc., com potencial de
quebrar silenciosamente (breaking changes em SDKs do Google Cloud não são
raros). Recomendação: `pip freeze > requirements-lock.txt` a partir de um
ambiente que você validou funcionando, e usar esse lock file no build do
Cloud Run.

---

## 3. Configuração

### Variáveis lidas por `config.py` (via `pydantic-settings`)

Lista completa (33 campos), com presença em `.env` (real, local, não
commitado) e `.env.example`:

| Variável | Default no código | No `.env` real | No `.env.example` |
|---|---|---|---|
| `GCP_PROJECT_ID` | **obrigatório, sem default** | ✅ `seu-id-unico` | ✅ (placeholder `meu-projeto-gcp`) |
| `GCP_LOCATION` | `us-central1` | ✅ | ✅ |
| `GEMINI_MODEL_ID` | **obrigatório, sem default** | ✅ `gemini-3.6-flash` | ✅ `gemini-3.6-flash` |
| `DRY_RUN` | `true` | ✅ | ✅ |
| `FIRESTORE_COLLECTION` | `investigations` | ✅ | ✅ |
| `SUSPICIOUS_TOPIC_ID` | `suspicious-domain-detected` | ✅ | ✅ |
| `COMPLETED_TOPIC_ID` | `investigation-completed` | ✅ | ✅ |
| `ORCHESTRATOR_SUBSCRIPTION_ID` | `sub-orchestrator` | ✅ | ✅ |
| `EVIDENCE_SUBSCRIPTION_ID` | `sub-evidence` | ❌ ausente (usa default) | ✅ |
| `EVIDENCE_GCS_BUCKET` | `None` (calculado) | ❌ ausente (usa default) | ✅ (comentado) |
| `OTEL_ENABLED` | `true` | ❌ ausente (usa default `true`) | ✅ |
| `GEMINI_INPUT_PRICE_PER_MILLION_USD` | `0.75` | ❌ ausente | ✅ |
| `GEMINI_OUTPUT_PRICE_PER_MILLION_USD` | `3.75` | ❌ ausente | ✅ |
| `TAKEDOWN_SUBSCRIPTION_ID` | `sub-takedown` | ❌ ausente | ✅ |
| `TAKEDOWN_ACTIONS_COLLECTION` | `takedown_actions` | ❌ ausente | ✅ |
| `TAKEDOWN_RATE_LIMIT_COLLECTION` | `takedown_rate_limits` | ❌ ausente | ✅ |
| `TAKEDOWN_DAILY_RATE_LIMIT_PER_BRAND` | `20` | ❌ ausente | ✅ |
| `BRAND_SECURITY_TEAM_EMAIL` | `None` | ❌ ausente | ✅ (comentado, sem valor) |
| `METRICS_FIRESTORE_COLLECTION` | `metrics` | ❌ ausente | ✅ |
| `AGENT_REGISTRY_COLLECTION` | `agent_registry` | ❌ ausente | ✅ |
| `GEMMA_OLLAMA_BASE_URL` | `http://localhost:11434` | ❌ ausente | ❌ ausente de `.env.example` também |
| `GEMMA_MODEL_ID` | `gemma3:270m` | ❌ ausente | ❌ ausente |
| `GEMMA_BATCH_WINDOW_SECONDS` | `2.0` | ❌ ausente | ❌ ausente |
| `GEMMA_BATCH_MAX_SIZE` | `5` | ❌ ausente | ❌ ausente |
| `GEMMA_REQUEST_TIMEOUT_SECONDS` | `15.0` | ❌ ausente | ❌ ausente |
| `GEMMA_MAX_RETRIES` | `1` | ❌ ausente | ❌ ausente |
| `TRIAGE_DISCARD_COLLECTION` | `triage_discards` | ❌ ausente | ❌ ausente |
| `GEMMA_CLOUD_RUN_VCPU_COUNT` | `1.0` | ❌ ausente | ❌ ausente |
| `GEMMA_CLOUD_RUN_MEMORY_GIB` | `1.0` | ❌ ausente | ❌ ausente |
| `CLOUD_RUN_CPU_PRICE_PER_VCPU_SECOND_USD` | `0.000024` | ❌ ausente | ❌ ausente |
| `CLOUD_RUN_MEMORY_PRICE_PER_GIB_SECOND_USD` | `0.0000025` | ❌ ausente | ❌ ausente |

**Nenhuma quebra funcional** — todas as ausências acima têm default no
código. Mas **achado**: as 8 variáveis `GEMMA_*`/`TRIAGE_DISCARD_COLLECTION`/
`CLOUD_RUN_*` nunca foram documentadas em `.env.example`, mesmo tendo
default — inconsistente com o padrão que o próprio arquivo segue para
todo o resto (todo campo de `config.py` tem uma linha correspondente
comentada). E **`.env` real está desatualizado**: falta toda a seção de
Sprint 3–6 que `.env.example` já documenta — sintoma direto do mesmo
problema da seção 1 (o `.env` local nunca foi atualizado a par do código).

### Valores placeholder

- `.env.example`: `GCP_PROJECT_ID=meu-projeto-gcp` — placeholder explícito, correto por ser o *example*.
- `.env` real: `GCP_PROJECT_ID=seu-id-unico` — **não é um placeholder esquecido**: confirmado que é o projeto GCP real e ativo (`gcloud config list` mostra `project = seu-id-unico`, e todos os recursos reais — tópicos, Firestore, Cloud Run — existem sob esse ID). O nome é incomum (lê como "seu-id-único" em português) mas é genuíno.
- Nenhum outro valor com cara de placeholder esquecido encontrado em `.env`.

### Item (a): `GEMINI_MODEL_ID=gemini-3.6-flash`

**Como foi definido — honestamente**: o próprio `.env.example` já admite,
em comentário, que não foi verificado contra documentação oficial:

> "No momento em que este arquivo foi escrito a documentação pública
> trazia sinais conflitantes sobre qual é o Flash GA mais recente
> (candidatos vistos: `gemini-3-flash-preview`, `gemini-3.1-flash-lite`,
> `gemini-3.6-flash`) — valide antes de usar."

Ou seja: **foi uma escolha entre candidatos incertos, não uma confirmação
via documentação oficial ou teste real** — apesar do texto pedir
explicitamente para validar, isso nunca foi feito antes de ir para
`.env`/`.env.example`.

**Reproduzi o erro ao vivo, agora, com credenciais reais** (`gcloud auth
application-default print-access-token` funcionou, projeto
`seu-id-unico`):

```python
client = genai.Client(vertexai=True, project='seu-id-unico', location='us-central1')
client.models.generate_content(model='gemini-3.6-flash', contents='diga oi', ...)
```
```
ClientError: 404 NOT_FOUND. {'error': {'code': 404, 'message':
'Publisher model `projects/seu-id-unico/locations/us-central1/publishers
/google/models/gemini-3.6-flash` was not found or your project does not
have access to it. ...'}}
```

**Reproduzido 2 vezes**, ~4h30 de diferença entre a primeira e a segunda
tentativa — consistente, não é flakiness momentânea.

**Achado curioso e não totalmente explicado**: existe UM documento real em
`investigations/nubank-seguro-login-teste.com` no Firestore, com
`investigated_at = 2026-08-25T22:09:53Z`, `model: 'gemini-3.6-flash'`,
`input_tokens: 850`, `output_tokens: 60`, e um `reasoning` coerente e
específico em português — parece ser uma chamada real que funcionou há
poucas horas. **NÃO CONSEGUI EXPLICAR essa contradição com certeza** —
hipóteses, nenhuma confirmada: (i) o modelo esteve disponível mais cedo
hoje e o acesso foi revogado/mudou depois; (ii) o campo `model` só
reflete `settings.gemini_model_id` no momento da chamada
(`llm_client.py::_extract_usage` grava `model_id=settings.gemini_model_id`,
**nunca** o que a API de fato confirma ter servido) — então o campo por si
só não prova que a chamada teve sucesso com esse modelo específico, mas
não explica os tokens/reasoning coerentes; (iii) o documento foi editado
manualmente. Note também `decision_rationale: 'fdjvhjfghb'` no mesmo
documento — claramente digitado à mão, sinal de que essa foi uma execução
manual de teste, não um fluxo automatizado real.

**Testei outros IDs de modelo, ao vivo, contra o mesmo projeto/região agora:**

| Modelo testado | Resultado |
|---|---|
| `gemini-3.6-flash` | ❌ 404 |
| `gemini-3-flash-preview` | ❌ 404 |
| `gemini-3.1-flash-lite` | ❌ 404 |
| `gemini-3-flash` | ❌ 404 |
| `gemini-3-pro` / `gemini-3-pro-preview` | ❌ 404 |
| `gemini-2.0-flash` / `gemini-2.0-flash-001` / `gemini-2.0-flash-lite` | ❌ 404 |
| `gemini-1.5-flash` / `gemini-1.5-flash-002` | ❌ 404 |
| **`gemini-2.5-flash`** | ✅ **SUCESSO** (respondeu "ok") |
| **`gemini-2.5-flash-lite`** | ✅ **SUCESSO** |
| **`gemini-2.5-pro`** | ✅ **SUCESSO** |

Nenhuma família "gemini-3.x" respondeu com sucesso neste projeto agora —
só a família 2.5. Isso não prova que "gemini-3.x" não existe em lugar
nenhum (pode ser preview/allowlist/outra região) — só que **não está
acessível neste projeto, nesta região, agora**. `gemini-2.5-flash` **não**
satisfaz literalmente o requisito do CLAUDE.md ("Gemini 3.5 Flash ou mais
recente") — é uma geração anterior.

**Comando exato para você confirmar de forma autoritativa** (a fonte
correta é o console, não uma lista via API que eu consegui montar aqui —
tentei via REST e o endpoint que usei devolveu erro genérico do Google,
não uma lista utilizável):

```bash
# 1. Console (mais confiável) — Vertex AI Model Garden, filtrar por "Gemini":
open "https://console.cloud.google.com/vertex-ai/model-garden?project=seu-id-unico"

# 2. Ou, achando um ID candidato, teste direto (o que eu fiz aqui):
python3 -c "
from google import genai
client = genai.Client(vertexai=True, project='seu-id-unico', location='us-central1')
r = client.models.generate_content(model='SEU_CANDIDATO_AQUI', contents='oi')
print(r.text)
"
```

---

## 4. Cobertura dos Sprints 0–6

Legenda: ✅ ATENDIDO · 🟡 PARCIAL · ❌ NÃO ATENDIDO · ⚪ NÃO VERIFICÁVEL AQUI

### Sprint 0–1 — Ingestão + Prefiltro (`plane1_ingestion/`)

| Critério | Status | Evidência |
|---|---|---|
| WebSocket certstream, extração de domínios | 🟡 PARCIAL | Código existe (`ct_listener.py`, 433 linhas) e importa sem erro; **nunca rodei contra o stream real** nesta auditoria (não é o objetivo aqui, e o processo não está deployado — ver seção 6) |
| Prefiltro determinístico (homoglyph, Levenshtein, allowlist) | 🟡 PARCIAL | Código existe e é usado por `takedown_agent.py`/tinha uso indireto testado via `test_ct_listener_triage_integration.py`, mas **zero teste unitário dedicado a `prefilter.py`** (achado da seção 1) — "implementado mas cobertura de teste direta inexistente" |
| Publica em `suspicious-domain-detected` | ✅ ATENDIDO | Tópico real confirmado: `gcloud pubsub topics list` lista `suspicious-domain-detected` |

### Sprint 2 — Orquestrador (cache-first, scraping, Gemini)

| Critério | Status | Evidência |
|---|---|---|
| Cache-first via Firestore | 🟡 PARCIAL | Código presente (`_get_cached_investigation`), mas sem teste dedicado que exercite esse caminho (só 1 teste no arquivo de registry) |
| Scraping determinístico truncado | 🟡 PARCIAL | Código presente, sem teste direto de `scrape_website` |
| Classificação via Gemini, saída Pydantic | 🟡 PARCIAL | Arquitetura correta (`response_schema=AnalysisResult`), **mas o `GEMINI_MODEL_ID` configurado retorna 404 agora** (seção 3) — sem um ID válido, esta etapa quebra em produção hoje |
| Publica `investigation-completed` | ✅ ATENDIDO | Tópico real confirmado via `gcloud pubsub topics list` |

### Sprint 2.5 — Triagem Gemma

| Critério | Status | Evidência |
|---|---|---|
| Lote, fail-open, custo medido | ✅ ATENDIDO | `tests/test_gemma_triage.py` (7 testes) + `tests/test_ct_listener_triage_integration.py` (3, fail-open com serviço derrubado de verdade) passam; métricas documentadas em `FINDINGS.md` |
| Serving real (Ollama/Cloud Run) | ⚪ NÃO VERIFICÁVEL AQUI | Ollama não está rodando neste ambiente (`curl localhost:11434` falhou); nenhum Cloud Run service de Gemma existe (`gcloud run services list` só mostra `sentinel-dashboard`) — nunca deployado de verdade |

### Sprint 3 — Agent Registry & Identity

| Critério | Status | Evidência |
|---|---|---|
| `registry.py`: publish/get/list/deprecate/invoke_agent | ✅ ATENDIDO | `tests/test_registry.py` (17 testes) passam |
| `seed_registry.py` publica os 4 manifestos | ✅ ATENDIDO **e confirmado em produção real** | `agent_registry` no Firestore real tem `ct-listener@1.0.0`, `orchestrator@1.0.0`, `evidence-collector@1.0.0` (DEPRECATED) e `@2.0.0` (ACTIVE), `takedown-agent@1.0.0` — todos ACTIVE exceto o deprecado, exatamente como o código prevê |
| Terraform: 1 SA por agente, permissão mínima | 🟡 PARCIAL | Ver seção 6 — parte aplicada, parte (takedown-sa) **não aplicada** |
| **Nada disto está commitado no git** | ❌ | Ver seção 1 |

### Sprint 4 — Evidence Collector

| Critério | Status | Evidência |
|---|---|---|
| Coleta determinística (DNS/hosting/TLS/RDAP/screenshot/fingerprint) | 🟡 PARCIAL | `tests/test_evidence_agent.py` (18 testes) passam — mockados. Screenshot real: **Playwright não consegue lançar o Chromium neste ambiente** (`libnspr4.so` ausente no sistema) — capacidade de screenshot está quebrada aqui |
| Sanitização antes de persistir (regra CLAUDE.md #5) | ✅ ATENDIDO (por teste) | Testado em `test_evidence_agent.py` |
| Publicado no registry, rodando contra Firestore real | 🟡 PARCIAL | Manifesto publicado (confirmado); execução real produziu 1 documento (`nubank-seguro-login-teste.com`) com `evidence` preenchido, mas `screenshot: None` com `is_partial: False` e `collection_errors: []` — **inconsistente com a lógica do código atual** (uma falha de screenshot deveria gerar `CollectionError` e `is_partial=True`). Não investiguei a fundo a origem exata deste documento — pode ser de uma execução anterior a alguma mudança de código, ou parcialmente montado à mão. Sinal adicional: `decision_rationale: 'fdjvhjfghb'` no mesmo fluxo (claramente digitado à mão) sugere teste manual, não pipeline 100% automático |

### Sprint 5 — Dashboard

| Critério | Status | Evidência |
|---|---|---|
| Fila de revisão, aprovação grava Firestore + publica Pub/Sub | ✅ ATENDIDO **e confirmado em produção real** | O documento real `nubank-seguro-login-teste.com` tem `status: TAKEDOWN_APPROVED`, `approved_by: felipe.inserti@gmail.com`, `takedown_channel: registrar_abuse` — a aprovação aconteceu de verdade, pelo fluxo real do dashboard |
| Deploy no Cloud Run | ✅ ATENDIDO | `gcloud run services list` confirma `sentinel-dashboard`, região `us-central1`, URL ativa, último deploy 2026-08-25T22:17:51Z por `felipe.inserti@gmail.com` |
| Testes automatizados do dashboard | ❌ NÃO ATENDIDO | `dashboard/package.json` não tem script `test` (só `dev`/`build`/`start`/`lint`) — zero teste automatizado |
| **Nada disto está commitado no git** | ❌ | Ver seção 1 — inclusive o Dockerfile/cloudbuild.yaml que geraram o deploy real |

### Sprint 6 — Takedown Agent

| Critério | Status | Evidência |
|---|---|---|
| `takedown_agent.py`: LLM decide canal dentro da categoria aprovada, execução determinística | ✅ ATENDIDO (por teste) | `tests/test_takedown_agent.py` (43 testes) + `tests/test_injection_cannot_redirect.py` (7 mockados) passam |
| Prova adversarial (injeção não redireciona) | ✅ ATENDIDO (por teste, mockado) | 7/7 cenários mockados passam; achado real corrigido (`_is_single_valid_contact`) — ver `docs/adversarial_report.md`. **3 cenários equivalentes contra o Gemini real (`-m live_llm`) nunca foram executados** — e agora sabemos que rodariam contra um `GEMINI_MODEL_ID` quebrado (404) se você tentasse hoje sem primeiro corrigir o item (a) |
| DRY_RUN sempre true, nada enviado | ✅ ATENDIDO (por teste) | Testado explicitamente |
| Execução real contra a fila de aprovação real | ⚪ NÃO VERIFICÁVEL AQUI / provavelmente NÃO ATENDIDO | Não existe processo `takedown_agent.py` rodando; a subscription `sub-takedown` real não tem nenhuma mensagem consumida (não verifiquei profundidade de fila, mas não há service account com permissão suficiente para processar — ver seção 6); Firestore não tem nenhuma coleção `takedown_actions`/`takedown_rate_limits` ainda — **confirma que este agente nunca rodou de verdade contra a infraestrutura real** |
| IAM de `takedown-sa` (Firestore + Vertex AI) aplicada | ❌ NÃO ATENDIDO | Ver seção 6 — só está no `.tf`, nunca aplicada |
| **Nada disto está commitado no git** | ❌ | Ver seção 1 |

---

## 5. Requisitos da trilha (Fortified Enterprise Fleet)

**Aviso de honestidade**: não tenho acesso direto ao texto oficial e
completo da rubrica do hackathon — só ao que você nomeou na pergunta e ao
que `CLAUDE.md` descreve. O mapeamento abaixo é minha melhor
correspondência entre esses nomes e o que existe no código; não é uma
confirmação de que atende à definição oficial de cada item.

| Item da trilha | O que existe | Onde | O que falta |
|---|---|---|---|
| **Agent Registry** | `registry.py` (`AgentManifest`, publish/get/list/deprecate/invoke_agent) + `seed_registry.py`. Publicado de verdade no Firestore real (5 manifestos) | `registry.py`, `seed_registry.py` | Nada commitado no git (seção 1) |
| **Agent Runtime** | Cada agente roda como processo Python long-lived consumindo Pub/Sub (`ct_listener.py`, `orchestrator.py`, `evidence_agent.py`, `takedown_agent.py`) | raiz + `plane1_ingestion/`/`plane2_agents/` | **Nenhum desses processos está deployado em lugar nenhum** (nem Cloud Run, nem qualquer outro runtime) — só rodam se alguém executar `python arquivo.py` manualmente |
| **Memory Bank** | ❌ Não encontrado | — | `grep -rniE "memory bank"` não retornou nada no código. Se a trilha exige um componente nomeado assim, **não existe** |
| **Agent Identity** | Terraform (`infra/main.tf`): 5 Service Accounts, uma por agente | `infra/main.tf` | IAM de `takedown-sa` desatualizada (seção 6); infra inteira não commitada |
| **Agent Gateway** | ❌ Não encontrado | — | `grep -rniE "agent gateway"` não retornou nada. Não há nenhum componente de gateway/proxy centralizando chamadas entre agentes — cada um fala direto com Pub/Sub/Firestore |
| **Model Armor** | Investigado, não integrado — decisão documentada | `sanitizer.py` (comentário "Model Armor (Google Cloud) -- pesquisa registrada por requisito") | Nunca instalado nem chamado; `sanitizer.py` + regras de revisão humana obrigatória fazem esse papel hoje |
| **Agent Observability** | `telemetry.py`: OpenTelemetry (traces + contadores), logging JSON estruturado com trace/span ID | `telemetry.py` | **Exportadores OTLP não instalados** (seção 2) — spans/métricas são criados mas não chegam ao Cloud Trace/Monitoring de verdade neste ambiente. Confirmado ao vivo: `GoogleCloudResourceDetector` falha no import ao rodar `telemetry.setup()` |
| **Gemini 3.5+ via Vertex AI** | `llm_client.py` usa `google-genai` em modo Vertex | `llm_client.py` | **`GEMINI_MODEL_ID` atual (`gemini-3.6-flash`) não existe/não está acessível** (seção 3) — nem esse nem nenhuma variante "gemini-3.x" testada funcionou; só a família 2.5 respondeu |
| **Framework Google (GenAI SDK / ADK)** | `google-genai` usado (`llm_client.py`, único ponto de contato) | `llm_client.py` | ADK **não é usado em nenhum lugar** (`grep` confirma, pacote nem instalado) — CLAUDE.md aceita "e/ou", então isso por si só não é uma falha, só uma observação |
| **Serviço GCP (Pub/Sub, Firestore, Cloud Run, Cloud Storage)** | Todos os 4 usados no código e **existem de verdade**: 3 tópicos + 3 subscriptions Pub/Sub, Firestore com 3 coleções reais, bucket de evidência + bucket `run-sources`, 1 serviço Cloud Run (`sentinel-dashboard`) | — | Só 1 dos ~5 processos do pipeline (o dashboard) está de fato em Cloud Run — os outros 4 nunca foram deployados |
| **Integração Gemma** | `gemma_triage.py` + `eval_triage.py`, métricas reais medidas contra Gemma 3 270M real (FINDINGS.md) | `gemma_triage.py` | Não deployado (nem Ollama local rodando aqui, nem Cloud Run do Gemma) |

---

## 6. Lacuna entre código e nuvem

O que existe como **código** mas **não está provisionado/deployado de
verdade**, confirmado com comandos reais contra o projeto `seu-id-unico`:

| Item | Estado no código | Estado real (GCP) |
|---|---|---|
| IAM de `takedown-sa` (`roles/datastore.user`, `roles/aiplatform.user`) | Presente em `infra/main.tf` (editado às 22:50) | **Ausente** — `gcloud projects get-iam-policy ... --filter="bindings.members:takedown-sa"` devolve vazio. `terraform.tfstate` é de 18:48, **4h mais antigo** que a última edição do `.tf` — `terraform apply` nunca rodou depois dessa mudança |
| `terraform apply` de qualquer mudança recente | `infra/main.tf` tem os comentários/bindings corrigidos | `terraform` **não está nem instalado** neste ambiente (`which terraform` vazio) — o `.tfstate` presente foi gerado em outra sessão/máquina |
| Processos `ct_listener.py`/`orchestrator.py`/`evidence_agent.py`/`takedown_agent.py` rodando continuamente | Código completo, testado (mockado) | **Nenhum deployado** — `gcloud run services list` só mostra `sentinel-dashboard`. Não há Cloud Run/GCE/GKE/Cloud Functions para nenhum dos 4 consumidores de Pub/Sub |
| Coleções `takedown_actions`, `takedown_rate_limits` | Usadas por `takedown_agent.py` | **Nunca criadas** — `db.collections()` real só mostra `agent_registry`, `investigations`, `metrics`. Confirma que `takedown_agent.py` nunca processou uma mensagem real |
| Gemma 3 270M em Cloud Run (`scripts/deploy_gemma_cloudrun.sh`) | Script existe | Não deployado; Ollama local também não está rodando aqui |
| Playwright (screenshot do evidence-collector) | Código completo | Chromium baixado mas **não consegue rodar** neste ambiente: `libnspr4.so: cannot open shared object file` — dependência de sistema (`apt`) ausente. Se o mesmo vale para onde você grava a demo, screenshots vão falhar silenciosamente (viram `CollectionError`, o bundle fica parcial) |
| GitHub (`origin/master`) | — | Só tem o commit inicial (Sprints 0–2.5) — ver seção 1 |

**O que está de fato provisionado e real, confirmado agora:**
- Tópicos Pub/Sub: `suspicious-domain-detected`, `investigation-completed`, `takedown-approved`.
- Subscriptions: `sub-orchestrator` (via `scripts/setup_gcp.sh`, sem label Terraform), `sub-evidence` e `sub-takedown` (`goog-terraform-provisioned: true`).
- Firestore: coleções `agent_registry` (5 manifestos), `investigations` (1 documento real), `metrics` (`pipeline_totals` com contadores reais, não-zero).
- Cloud Storage: bucket `seu-id-unico-sentinel-evidence` (Terraform) e `run-sources-seu-id-unico-us-central1` (Cloud Run build).
- 5 Service Accounts (`ct-listener-sa`, `orchestrator-sa`, `evidence-sa`, `takedown-sa`, `dashboard-sa`), mas com bindings desatualizados para `takedown-sa`.
- Índice composto do Firestore para a fila de revisão do dashboard — `READY`.
- Cloud Run: `sentinel-dashboard`, deployado e ativo.
- APIs habilitadas: `aiplatform`, `firestore`, `pubsub`, `storage` (x3), `run`, `cloudtrace`, `monitoring`, `logging`, `bigquerystorage` — todas as que o projeto precisa **estão ligadas**.

---

## 7. Riscos (ordem de gravidade)

1. **Nada dos Sprints 3–6 está no git/GitHub.** Se a submissão for avaliada
   pelo repositório (o cenário mais provável num hackathon), o avaliador
   vê literalmente o projeto do Sprint 2.5 — sem Agent Registry, sem
   evidence collector, sem dashboard, sem takedown agent, sem a prova
   adversarial. É a diferença entre "trilha atendida" e "trilha não
   atendida" na prática. Isso também significa que **todo o trabalho
   existe hoje só neste working tree** — qualquer perda deste ambiente
   (reset de sandbox, disco, etc.) apaga tudo que não está commitado.
2. **`GEMINI_MODEL_ID` não funciona.** Confirmado ao vivo, reproduzido 2x.
   Sem corrigir isso, `orchestrator.py` (a espinha dorsal do Plano 2) e
   `takedown_agent.py` (`select_channels`/`draft_notice`) falham toda
   chamada real ao Gemini — incluindo qualquer demonstração ao vivo.
3. **Nenhum dos 4 agentes de Pub/Sub está deployado.** Uma gravação de
   demo que dependa de "o pipeline reagindo sozinho" (certstream →
   prefiltro → Gemma → orchestrator → evidence → dashboard) não vai
   acontecer sozinha — precisa rodar cada `python arquivo.py` manualmente,
   em paralelo, ao vivo ou nos bastidores antes da gravação.
4. **IAM de `takedown-sa` desatualizada em relação ao código.** Se você
   tentar rodar `takedown_agent.py` de verdade contra o projeto real hoje,
   ele vai falhar com `PermissionDenied` no primeiro acesso a
   Firestore/Vertex AI — precisa de `terraform apply` (e `terraform` nem
   está instalado neste ambiente; verifique se está na sua máquina).
5. **Playwright sem dependência de sistema.** Se o ambiente de gravação
   for igual a este, o screenshot do evidence-collector nunca aparece
   (falha graciosa, mas visivelmente ausente no dashboard).
6. **Exportadores OTel ausentes.** "Agent Observability" da trilha, se
   avaliada olhando o Cloud Trace/Monitoring de verdade, vai aparecer
   vazio — mesmo com todo o código de instrumentação correto.
7. **Cobertura de teste real mais fina do que parece.** `prefilter.py`
   (o coração da tese de token economy) e a maior parte de
   `orchestrator.py` não têm teste dedicado — os 131 testes que passam
   provam principalmente Sprints 3–6 e a camada Gemma, não a ingestão
   original.
8. **`requirements.txt` sem versão fixada + `httpx` não declarado.** Risco
   de build quebrar de forma diferente em cada ambiente/dia.
9. **Documento real de investigação com inconsistência não explicada**
   (`screenshot: None` + `is_partial: False` + `collection_errors: []`,
   e `model: gemini-3.6-flash` funcionando quando não funciona mais
   agora) — sinal de que pelo menos uma execução real foi parcialmente
   manual/editada, não 100% pipeline automático. Não é grave por si só,
   mas indica que "rodei contra recurso real uma vez" não é o mesmo que
   "o pipeline automático funciona ponta a ponta".
