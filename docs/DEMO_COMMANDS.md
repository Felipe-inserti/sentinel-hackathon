# Comandos da demo (gravação) — ordem exata

Preparado na noite de 2026-08-28 para a gravação de manhã. Todo item abaixo
tem uma marca:

- **[PRONTO]** — verificado por execução nesta sessão, já funciona.
- **[EU RODO AMANHÃ]** — comando pronto, mas exige credencial/decisão sua
  (senha, projeto real, OAuth) que esta sessão não tinha. Execute você e me
  cole o resultado se algo quebrar.
- **[NÃO VERIFICADO]** — não pôde ser testado nesta sessão (falta infra ou
  credencial); comando abaixo é o melhor palpite, confirme antes de contar
  com ele no vídeo.

## CORREÇÃO (2026-08-28, sprint multimodal): o projeto errado ficou
## documentado aqui por uma sessão inteira

Uma versão anterior desta seção afirmava que o projeto real era
`sentinel-hack-felipe` ("achei testando os 5 projetos visíveis na conta").
Isso estava **errado** — `sentinel-hack-felipe` é um projeto vazio, nunca
usado por este pipeline. O projeto real é **`seu-id-unico`** — não é um
placeholder apesar do nome parecer um: é o project ID literal, criado
assim, e é onde os 4 Cloud Run Jobs, o Cloud Scheduler e o Firestore deste
projeto de fato rodam (confirmado via `gcloud config get-value project`,
`terraform plan` contra o state real, e `gcloud services list`). `.env`
já tinha `GCP_PROJECT_ID=seu-id-unico` correto o tempo todo — o erro foi
só desta documentação, nunca do `.env` real.

**Como isso foi descoberto:** ao verificar se `aiplatform.googleapis.com`
estava habilitado antes de rodar uma chamada real ao Gemini, uma sessão
seguinte confiou neste documento em vez de reconferir contra o projeto
efetivamente configurado no `gcloud` local — checou o projeto errado,
concluiu (também errado) que Vertex AI não estava habilitado. Corrigido
na hora: `aiplatform.googleapis.com` **já estava** habilitado em
`seu-id-unico` desde antes. Lição registrada como regra permanente: nunca
inferir o project ID pelo nome do arquivo/doc — sempre confirmar contra
`gcloud config get-value project` e/ou o `terraform.tfstate` real antes de
qualquer chamada.

Todas as ocorrências de `sentinel-hack-felipe` abaixo foram corrigidas
para `seu-id-unico`.

**Segunda correção, mais importante:** a tabela abaixo (e a seção
"Boa notícia") descrevia `seu-id-unico` como quase vazio -- estava
descrevendo o projeto ERRADO (`sentinel-hack-felipe`). Reverificado agora,
por execução direta (`gcloud pubsub/run/scheduler/storage list` +
Firestore real), contra o projeto correto:

| Recurso | Estado real em `seu-id-unico` |
|---|---|
| Tópicos Pub/Sub (`suspicious-domain-detected`, `investigation-completed`, `takedown-approved`) | ✅ os 3 existem |
| Subscriptions (`sub-orchestrator`, `sub-evidence`, `sub-takedown`) | ✅ as 3 existem |
| Firestore (native) | ✅ existe, **NÃO vazio** -- `agent_registry` seedado (`ct-listener@1.0.0`, `evidence-collector@1.0.0`, `brand-agent-{nubank,loggi,ifood}@1.0.0`, ...), `investigations` com dossiês reais (aparentam ser do run de observação de 48h citado no histórico do projeto -- domínios reais capturados, não sintéticos) |
| Bucket GCS de evidência (`seu-id-unico-sentinel-evidence`) | ✅ existe |
| Cloud Run Jobs (`ct-listener-job`, `orchestrator-job`, `evidence-collector-job`, `takedown-agent-job`) | ✅ os 4 existem |
| Cloud Run Services | ✅ `sentinel-agent-gateway` e `sentinel-dashboard` existem |
| Cloud Scheduler (`sentinel-run-{ct-listener,orchestrator,evidence-collector,takedown-agent}`) | ✅ os 4 existem |

**Isto muda o plano de verdade:** as seções 1 e 2 abaixo ("provisionar o
que falta" / "seed do Agent Registry") descrevem um projeto vazio que
**não é mais o estado real** -- tópicos, subscriptions, bucket e registry
já existem. Não fiz uma auditoria linha a linha do resto deste documento
(seções 3-6) para reescrevê-las com o estado atual -- isso é mais do que
foi pedido nesta correção pontual. Antes de seguir qualquer passo abaixo
marcado **[EU RODO AMANHÃ]**, confirme se ele ainda é necessário (pode já
estar feito) em vez de assumir o texto como está.

---

## 0. Hoje à noite — só você (credenciais que esta sessão não tem)

### 0.1 `.env` do pipeline Python — já está correto, nada a trocar

`GCP_PROJECT_ID=seu-id-unico` **não é um placeholder** (ver correção
acima) — é o project ID real, e o `.env` já está assim. Confirme só o
resto:

```bash
GCP_PROJECT_ID=seu-id-unico
GCP_LOCATION=global

DEMO_INSECURE_HTTP=true
DEMO_LOCAL_HTTP_PORT=8000
```

### 0.2 `dashboard/.env.local` — mesmo projeto, mais o OAuth Client ID

```bash
GCP_PROJECT_ID=seu-id-unico
NEXT_PUBLIC_GCP_PROJECT_ID=seu-id-unico
EVIDENCE_GCS_BUCKET=seu-id-unico-sentinel-evidence
SESSION_SECRET=$(openssl rand -base64 32)
```

`GOOGLE_CLIENT_ID`/`NEXT_PUBLIC_GOOGLE_CLIENT_ID` estão **vazios** — sem
isso o login do dashboard não funciona, e o passo de aprovação do vídeo
depende do dashboard. Passo manual único (não automatizável, confirmado no
`dashboard/README.md`):

1. Abra <https://console.cloud.google.com/auth/clients?project=seu-id-unico>
2. Se pedir, configure a tela de consentimento: tipo **Externo**, nome
   "Sentinel", seu e-mail como contato — não precisa publicar.
3. Crie um **Client ID** tipo **Web application**.
4. Em "Authorized JavaScript origins", adicione `http://localhost:3000`.
5. Copie o Client ID em `GOOGLE_CLIENT_ID` e `NEXT_PUBLIC_GOOGLE_CLIENT_ID`.
6. Preencha `ALLOWED_REVIEWER_EMAILS=felipe.inserti@gmail.com` (ou
   `ALLOWED_REVIEWER_DOMAIN`) — sem isso ninguém consegue aprovar nada.

### 0.3 Senha de app do Gmail (para o SMTP da demo)

1. Ative verificação em 2 etapas na conta que vai enviar (se ainda não
   tiver): <https://myaccount.google.com/security>
2. Em <https://myaccount.google.com/apppasswords>, gere uma senha de app
   nova (nome sugerido: "Sentinel Demo SMTP").
3. Copie a senha de 16 caracteres (sem espaços) para o `.env`:

```bash
DEMO_SMTP_HOST=smtp.gmail.com
DEMO_SMTP_PORT=587
DEMO_SMTP_USERNAME=seu-email@gmail.com
DEMO_SMTP_PASSWORD=<senha de app, 16 chars>
DEMO_LIVE_SEND_ALLOWLIST={"demo-teste.sentinel.local": "seu-email@gmail.com"}
```

`DEMO_LIVE_SEND_ALLOWLIST` é `dict[str, str]` no `config.py`
(`pydantic-settings`) — via env var precisa ser um JSON válido numa linha
só, como acima. O domínio na chave tem que bater **exatamente** com o
domínio que você vai injetar no Pub/Sub no passo 3.

### 0.4 `/etc/hosts` — mapear o domínio de teste para localhost

```bash
echo "127.0.0.1 demo-teste.sentinel.local" | sudo tee -a /etc/hosts
```

**Aviso para não se confundir no meio da gravação:** isso faz o
`requests.get()`/HTTP funcionar contra `localhost`, mas **não** faz o
`evidence_agent.py` resolver DNS/A-record de verdade — `dnspython`
consulta um nameserver de rede, não lê `/etc/hosts`. Resultado esperado
(não é bug): a seção DNS/hosting do dossiê de evidência fica vazia,
`is_partial=True` — comportamento de "falha graciosa" já documentado no
próprio módulo, não precisa explicar como erro no vídeo.

---

## 1. [EU RODO AMANHÃ] — provisionar só o que falta (gcloud, não terraform)

Terraform completo tentaria também criar Cloud Run + Artifact Registry
(exige imagens já buildadas, que não existem) e o alerta de billing — mais
do que o necessário e contra a regra "sem gastar". Os comandos abaixo criam
só os 2 tópicos + 3 subscriptions + 1 bucket que faltam, com os MESMOS
nomes que `config.py` já usa por default:

```bash
P=seu-id-unico

gcloud pubsub topics create investigation-completed --project=$P
gcloud pubsub topics create takedown-approved --project=$P

gcloud pubsub subscriptions create sub-orchestrator \
  --topic=suspicious-domain-detected --project=$P
gcloud pubsub subscriptions create sub-evidence \
  --topic=investigation-completed --project=$P
gcloud pubsub subscriptions create sub-takedown \
  --topic=takedown-approved --project=$P

gcloud storage buckets create gs://seu-id-unico-sentinel-evidence \
  --project=$P --location=us-central1
```

Confirme antes de gravar:
```bash
gcloud pubsub subscriptions list --project=$P --format="value(name)"
gcloud storage buckets list --project=$P --format="value(name)"
```

## 2. [EU RODO AMANHÃ] — Agent Registry (registry.py) precisa dos manifestos publicados

`registry.invoke_agent` rejeita qualquer payload se não houver versão
`ACTIVE` publicada para o agente — Firestore está vazio agora (bucket
criado do zero no passo 1). Rode o seed antes de publicar qualquer
mensagem:

```bash
source .venv/bin/activate   # ou o venv que preferir dos 3 que existem
python seed_registry.py
python seed_brand_agents.py   # opcional, só se for demonstrar BrandAgent
```

**[NÃO VERIFICADO]** — não rodei porque escreve no Firestore real do
projeto; comando é o que o próprio `seed_registry.py`/README descrevem.

## 3. [EU RODO AMANHÃ] — aquecer os serviços (cold start come segundos)

Em terminais separados, NA ORDEM (evidence-collector e orchestrator
importam `firestore.Client()`/`pubsub_v1` no nível de módulo — a conexão
inicial é o que trava; suba tudo e espere o log de "escutando em..." antes
de gravar):

```bash
# terminal 1 — página de teste
cd demo && python3 -m http.server 8000

# terminal 2 — orchestrator (Camada 2)
source .venv/bin/activate
python plane2_agents/orchestrator.py
# espera log: "Orchestrator escutando em projects/seu-id-unico/subscriptions/sub-orchestrator"

# terminal 3 — evidence-collector (Camada 4)
source .venv/bin/activate
python evidence_agent.py
# espera log: "Evidence collector escutando em .../sub-evidence"

# terminal 4 — takedown-agent (Camada 5), DRY_RUN=true (padrão do .env)
source .venv/bin/activate
python takedown_agent.py
# espera log: "Takedown agent escutando em .../sub-takedown (DRY_RUN=True)"

# terminal 5 — dashboard
cd dashboard && npm run dev
# espera "Ready" em http://localhost:3000
```

Ollama, se for demonstrar a triagem Gemma / fail-open:
```bash
ollama serve &   # se não estiver rodando como serviço já
ollama pull gemma3:270m   # se ainda não tiver o modelo local
```

## 4. [EU RODO AMANHÃ] — disparar a investigação (injeção direta no Pub/Sub)

O domínio de teste não passa pelo prefilter (não é isso que está sendo
testado aqui) — publica direto no tópico que o `sub-orchestrator` consome,
no formato de `SuspiciousDomainSignal` (`seed_registry.py`), só `domain` é
obrigatório:

```bash
gcloud pubsub topics publish suspicious-domain-detected \
  --project=seu-id-unico \
  --message='{"domain": "demo-teste.sentinel.local", "matched_brand": null}'
```

Isso deve, em segundos: orchestrator raspar `http://demo-teste.sentinel.local:8000`
(via `DEMO_INSECURE_HTTP=true`), sanitizer.py redigir os e-mails de ataque
e detectar `ignore_previous_instructions`, Gemini classificar (o próprio
system prompt trata tentativa de manipulação como sinal MALICIOUS),
`investigation-completed` disparar o evidence-collector, que tenta
screenshot/DNS/TLS/RDAP (DNS/RDAP devem falhar — domínio fake, ver aviso
do passo 0.4 — bundle fica `is_partial=True`, normal).

**[NÃO VERIFICADO]** — cadeia inteira depende da infra do passo 1 e do
Gemini real; não pude rodar contra o projeto de verdade nesta sessão.

## 5. [PRONTO] — verificado nesta sessão, sem depender de infra

Já confirmei por execução (Playwright real + BeautifulSoup real +
`sanitizer.sanitize()` real, `.venv/bin/python`, sem GCP):

- os 3 vetores do `demo/index.html` aparecem no view-source e ficam
  invisíveis no render (`page.inner_text` do Playwright)
- vetor 1 (CSS oculto) chega ao scraper do orchestrator e é pego por
  `injection_patterns_found: ['ignore_previous_instructions']`, e-mails
  redigidos como PII
- vetor 2 (comentário HTML) nunca chega ao scraper — `BeautifulSoup.stripped_strings`
  exclui nós `Comment` estruturalmente
- vetor 3 (Unicode Tag Characters) é apagado por completo pelo
  `sanitizer.py` (`_strip_invisible_and_private`, categoria Unicode `Cf`)
  antes de qualquer regex rodar — 0 caracteres sobrevivem
- os 3 testes do `DEMO_LIVE_SEND_ALLOWLIST` (`tests/test_takedown_agent.py`)
  passam: dentro da allowlist + `DRY_RUN=false` envia; fora da allowlist +
  `DRY_RUN=false` aborta com erro auditável; `DRY_RUN=true` nunca envia
  mesmo com domínio na allowlist

Suíte completa — rode de novo amanhã como último check antes de gravar
(não depende de GCP, tudo mockado):
```bash
source .venv/bin/activate
pytest tests/test_takedown_agent.py -q
```

## 6. [EU RODO AMANHÃ] — aprovar no dashboard

1. `http://localhost:3000` → login com Google (seu e-mail precisa estar em
   `ALLOWED_REVIEWER_EMAILS`, passo 0.2)
2. Fila de revisão → abrir `demo-teste.sentinel.local`
3. Aprovar takedown, categoria **`brand_protection_vendor`** (única que
   inclui `BRAND_SECURITY_TEAM` em `ALLOWED_CHANNELS_BY_CATEGORY` —
   irrelevante pra esse domínio específico, já que o envio real vai pelo
   caminho da allowlist, não pelo multi-canal do LLM, mas precisa de uma
   categoria válida pra `_load_verified_approval` aceitar)
4. Isso grava `status=TAKEDOWN_APPROVED` em `investigations/demo-teste.sentinel.local`
   e publica em `takedown-approved` → `takedown_agent.py` local processa,
   vê o domínio na `DEMO_LIVE_SEND_ALLOWLIST`, ignora RDAP/LLM
   completamente, manda o e-mail via `_send_demo_notification`.

## 7. [EU RODO AMANHÃ] — DRY_RUN=false só para este teste

Nunca deixe isso no `.env` persistente — exporte só no terminal do
`takedown_agent.py` antes de subir, ou reinicie o processo com a env var:

```bash
DRY_RUN=false python takedown_agent.py
```

Confirme visualmente no log: `"Takedown agent escutando em .../sub-takedown (DRY_RUN=False)"`.
Depois da demo, mate esse processo e suba de novo com `DRY_RUN=true` (ou
simplesmente não exporte a variável) antes de continuar mexendo no projeto.

## 8. [NÃO VERIFICADO] — confirmar e-mail chegando de verdade

```bash
# nada a rodar aqui além de olhar a caixa de entrada configurada em
# DEMO_LIVE_SEND_ALLOWLIST — se não chegar em ~1 min, cheque:
#   - spam
#   - log do terminal 4 (takedown_agent.py) para o erro exato de smtplib
#   - senha de app correta (16 chars, sem espaço, gerada no passo 0.3)
```

## 9. Fail-open do Ollama (opcional, camada de triagem Gemma)

Isso é uma camada de `ct_listener.py` (Camada 1), que **não** está no
caminho do passo 4 (você injeta direto no Pub/Sub, pulando o prefilter e
a triagem). Pra demonstrar fail-open isoladamente, sem rodar o
`ct_listener` inteiro contra o CT stream real:

```bash
source .venv/bin/activate
python3 -c "
import asyncio
from gemma_triage import DomainSignals, triage_batch

signals = [DomainSignals(
    domain='demo-teste.sentinel.local', target_brand=None,
    similarity_score=0.9, heuristics_triggered=['typosquat'],
    domain_tokens=['demo','teste'], tld='local',
)]
outcome = asyncio.run(triage_batch(signals))
print('fallback_used:', outcome.fallback_used)
for d, r in outcome.results.items():
    print(d, '->', r.verdict, r.risk_score)
"
```

Rode uma vez com `ollama serve` no ar (deve dar `fallback_used: False`,
veredito real do Gemma), depois:
```bash
pkill ollama
```
e rode de novo (deve dar `fallback_used: True`, todo domínio vira
`INVESTIGATE` — fail-open, nunca fail-closed, nunca descarta silenciosamente
por o Ollama estar fora do ar).

**[NÃO VERIFICADO]** — Ollama não está disponível nesta sessão de análise;
comando é direto contra `gemma_triage.triage_batch`, mesma função que
`ct_listener.py` chama, sem precisar do CT stream real.

## 10. Números finais numa tela

Ver `docs/DEMO_NUMBERS.md` (próximo item a preparar) para os números de
baseline, trace_ids e o caso de injeção mais limpo pro cold open — depende
de consultar Firestore/Cloud Trace reais, não verificável nesta sessão.
