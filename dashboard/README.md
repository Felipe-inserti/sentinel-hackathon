# Sentinel Dashboard

Interface de human-in-the-loop do Sentinel: fila de revisão, detalhe de
investigação, painel de token economy e mapa de campanhas. Next.js 16 +
TypeScript + Tailwind v4, deployado como container no Cloud Run.

**Não inventa estrutura de dado nova.** Lê exatamente o que
`orchestrator.py`, `evidence_agent.py` e `registry.py` já gravam no
Firestore (`investigations`, `agent_registry`, `metrics/pipeline_totals`)
-- ver `src/lib/types.ts`. Os únicos campos novos são os que a própria
decisão humana precisa gravar (`approved_by`/`approved_at`/
`decision_rationale`/`takedown_channel`/`rejected_by`/`rejected_at`/
`rejection_reason`), acrescentados ao mesmo documento `investigations/{domain}`.

## Decisões de arquitetura (leia antes de mexer)

### Por que não é Firebase Auth / Firestore client SDK

A primeira tentativa foi Firebase Auth + Firestore client-side (`onSnapshot`
direto do navegador). A API de Management do Firebase (`addFirebase`)
recusou a chamada com `403 PERMISSION_DENIED` **mesmo com `roles/owner`**
no projeto -- sintoma de um aceite de Termos de Serviço do Firebase
pendente, que só existe pelo console (não encontrei caminho de API/CLI pra
contornar; a API antiga de OAuth brand do IAP que poderia servir de
alternativa está deprecada pelo próprio Google, shutdown anunciado pra
19/mar/2026).

Arquitetura final, sem Firebase:

- **Auth**: [Google Identity Services](https://developers.google.com/identity/gsi/web)
  ("Sign In With Google", só o widget + `google-auth-library` pra verificar
  o ID token no servidor). Só precisa de UM OAuth Client ID -- ver
  [Passo manual único](#passo-manual-único-oauth-client-id) abaixo.
- **Sessão**: cookie `httpOnly` assinado com HMAC-SHA256 via Web Crypto
  (`src/lib/session-cookie.ts`) -- sem tabela de sessão, sem Redis, sem
  dependência nova.
- **Tempo real**: em vez do Firestore client SDK (que exigiria regras de
  segurança + registrar um Web App no Firebase), os listeners
  (`onSnapshot`) rodam no **servidor** via `@google-cloud/firestore`
  (Admin) e são reempurrados pro navegador como Server-Sent Events (ver
  `src/lib/sse.ts` e `src/app/api/stream/*`). O navegador nunca fala
  direto com o Firestore.
- **Artefatos de evidência** (screenshot/HTML): nunca expostos como
  `gs://` cru -- `src/app/api/artifact/route.ts` faz proxy autenticado,
  validando que a URI pertence ao bucket de evidência configurado antes de
  servir.

### Nenhum PII renderizado em tela

Todo texto persistido pelo pipeline já passou por `sanitizer.py` antes de
chegar ao Firestore (regra CLAUDE.md #5) -- o dashboard não redige nada, só
exibe o que já chegou limpo. A única exceção documentada é o **screenshot**
(imagem, não dá pra redigir PII por regex): quando
`evidence.form_fields_detected.detected` é `true`, o componente
`EvidenceScreenshot` borra a imagem por padrão e exige um clique explícito
do revisor pra revelar (`src/components/EvidenceScreenshot.tsx`). O HTML
sanitizado nunca é renderizado inline (só download) -- reabrir esse HTML
num `<iframe>` reintroduziria o mesmo risco de rede que
`evidence_agent.py` tomou cuidado de isolar.

### Pendência conhecida: link exato pro Cloud Trace

O requisito pede link pro trace exato de cada investigação. Hoje
`orchestrator.py`/`evidence_agent.py` não persistem `trace_id` no
documento Firestore (só o log estruturado carrega isso, via
`telemetry.py::_JsonTraceFormatter`) -- persistir exigiria mudar o código
Python existente do pipeline, e a instrução deste sprint foi parar e
perguntar antes disso. `CloudTraceLink` hoje linka pro Cloud Trace
Explorer do projeto (não um deep-link exato) -- ver
`src/components/CloudTraceLink.tsx`. Se quiser o link exato, o próximo
passo é: `span.get_span_context().trace_id` carimbado no dossiê em
`_save_investigation`/`_update_dossier_with_evidence`.

### Mapa de campanhas -- MVP, não o Sprint 7

`/campaigns` agrupa domínios por `infrastructure_fingerprint.fingerprint_hash`
idêntico -- usa um dado que `evidence_agent.py` já calcula, sem
infraestrutura nova. Não é o clustering completo do Sprint 7 (que ainda
não existe): sem tempo real, sem similaridade parcial, só hash exato. Ver
`src/app/(app)/campaigns/page.tsx`.

### Permissões widened neste sprint

`dashboard-sa` (já existia em `infra/`) tinha só `roles/datastore.viewer`
(leitura). A fila de revisão grava a decisão no mesmo documento que lê, e o
proxy de evidência precisa ler o bucket -- `infra/main.tf` foi atualizado
para `roles/datastore.user` + `roles/storage.objectViewer` no bucket de
evidência. Ver `infra/README.md` (matriz de permissões atualizada).

### Achado: `.env` do pipeline Python está com projeto placeholder

`GCP_PROJECT_ID=meu-projeto-gcp` no `.env` real (não commitado) do
pipeline Python nunca foi preenchido -- o projeto real é `seu-id-unico`
("Sentinel Hackathon"). Como nenhum cliente do pipeline (`registry.py`,
`orchestrator.py`, `evidence_agent.py`) passa `project=` explícito pro
Firestore/Pub/Sub, ADC resolve pelo projeto padrão do `gcloud config` e
tudo funcionou por acidente até agora -- mas `topic_path`/`subscription_path`
usam `settings.gcp_project_id` explicitamente, então em Cloud Run (sem o
`gcloud config` local) isso vai apontar pro projeto errado e falhar. Não
mudei o `.env` do pipeline (fora do escopo deste sprint, e é a mesma regra
de "pare e pergunte antes de mexer no que já existe") -- só uso o valor
real (`seu-id-unico`) na configuração deste dashboard.

## Rodando localmente

```bash
cd dashboard
npm install
cp .env.example .env.local   # preencha GCP_PROJECT_ID=seu-id-unico e o resto
gcloud auth application-default login   # ADC local, mesma credencial do resto do projeto
npm run dev
```

Sem `GOOGLE_CLIENT_ID`/`NEXT_PUBLIC_GOOGLE_CLIENT_ID` preenchidos, tudo
sobe normalmente exceto o login (o botão "Sign In With Google" mostra um
aviso em vez de renderizar) -- ver seção abaixo.

## Passo manual único: OAuth Client ID

Único passo que não pude automatizar (tentei via `gcloud alpha iap
oauth-brands` -- API deprecada pelo próprio Google -- e via
`gcloud alpha identity-platform` -- grupo de comando inexistente nesta
versão do gcloud; ambos exigem o console):

1. Abra <https://console.cloud.google.com/auth/clients?project=seu-id-unico>
2. Se pedir, configure a "Tela de consentimento OAuth": tipo **Externo**,
   nome do app "Sentinel", seu e-mail como contato. Não precisa publicar
   (fica em modo de teste, suficiente pra demo/uso interno) -- adicione os
   e-mails dos revisores como "test users" se ficar em modo de teste.
3. Crie um **Client ID**, tipo **Web application**.
4. Em "Authorized JavaScript origins", adicione a URL do Cloud Run depois
   do primeiro deploy (ex: `https://sentinel-dashboard-xxxx.run.app`) e
   `http://localhost:3000` pra dev local.
5. Copie o Client ID (não precisa do secret -- GIS usa só o ID) em
   `GOOGLE_CLIENT_ID` e `NEXT_PUBLIC_GOOGLE_CLIENT_ID`.

## Deploy no Cloud Run

**Não use `gcloud run deploy --source . --set-build-env-vars=...`** --
tentei isso no primeiro deploy deste sprint e o serviço subiu "com
sucesso", mas os dois `NEXT_PUBLIC_*` ficaram vazios no bundle do
cliente: `--set-build-env-vars`/`--build-env-vars` só tem efeito quando o
Cloud Run builda via **Cloud Native Buildpacks** -- com um `Dockerfile`
presente (nosso caso), o `docker build` real que roda dentro do Cloud
Build ignora esse flag silenciosamente, sem erro nenhum. O sintoma: o
botão de login mostra "NEXT_PUBLIC_GOOGLE_CLIENT_ID não configurado"
mesmo com a env var setada no `gcloud run deploy`.

O jeito que funciona de verdade: buildar explicitamente via
`gcloud builds submit` com `cloudbuild.yaml` (já neste diretório), que
passa `--build-arg` de verdade pro `docker build`, e só então apontar o
Cloud Run pra imagem pronta:

```bash
cd dashboard

PROJECT_ID=seu-id-unico
REGION=us-central1
SESSION_SECRET=$(openssl rand -base64 32)
GOOGLE_CLIENT_ID="<client id criado acima>"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/sentinel-dashboard:latest"

# 1. Build com os NEXT_PUBLIC_* de verdade embutidos (cloudbuild.yaml):
gcloud builds submit \
  --project "$PROJECT_ID" \
  --config cloudbuild.yaml \
  --substitutions "_NEXT_PUBLIC_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},_NEXT_PUBLIC_GCP_PROJECT_ID=${PROJECT_ID},_IMAGE=${IMAGE}" \
  .

# 2. Deploy da imagem já buildada (rápido -- não builda de novo):
gcloud run deploy sentinel-dashboard \
  --image "$IMAGE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --service-account "dashboard-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},NEXT_PUBLIC_GCP_PROJECT_ID=${PROJECT_ID},SESSION_SECRET=${SESSION_SECRET},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},FIRESTORE_COLLECTION=investigations,AGENT_REGISTRY_COLLECTION=agent_registry,METRICS_FIRESTORE_COLLECTION=metrics,TAKEDOWN_TOPIC_ID=takedown-approved,EVIDENCE_GCS_BUCKET=${PROJECT_ID}-sentinel-evidence" \
  --allow-unauthenticated \
  --min-instances=0
```

Pra redeploys futuros que só mudam código server-side (nenhum
`NEXT_PUBLIC_*` novo), `gcloud run deploy --image "$IMAGE"` sozinho
já reaproveita as env vars da revisão anterior -- só repita o passo 1
quando algo que afeta o bundle do cliente mudar. Se quiser conferir que o
Client ID realmente foi embutido antes de anunciar o deploy como pronto:
baixe `/login`, ache os chunks `_next/static/chunks/*.js` no HTML, e
`grep` pelo Client ID neles.

Depois do primeiro deploy, volte no passo 4 da seção anterior e adicione a
URL `.run.app` real como "Authorized JavaScript origin", senão o botão de
login falha com `origin_mismatch`.

`--allow-unauthenticated` é sobre o Cloud Run aceitar requisições HTTP
sem IAM -- a autenticação de verdade (quem consegue *usar* o app) é o
Google Identity Services + o gate no `proxy.ts`, não o Cloud Run.
`dashboard-sa` já existe em `infra/` com permissão mínima (ver
`infra/README.md`); aplique `terraform apply` lá antes de deployar aqui se
ainda não rodou (cria/atualiza a Service Account, o bucket de evidência e
o índice -- este último via `gcloud`, ver abaixo, não Terraform).

### Índice composto do Firestore

A fila de revisão filtra por `classification` + `status` e ordena por
`confidence` -- exige um índice composto (já criado neste sprint no
projeto real; rode de novo se for outro projeto):

```bash
gcloud firestore indexes composite create \
  --collection-group=investigations \
  --field-config=field-path=classification,order=ascending \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=confidence,order=descending \
  --project="$PROJECT_ID"
```

## Estrutura

```
src/
  lib/
    types.ts              tipos espelhando os modelos Pydantic do pipeline
    gcp.ts                clientes Firestore/Pub/Sub (Admin, server-only)
    session-cookie.ts      assinatura HMAC do cookie (Edge-safe, Web Crypto)
    session.ts              verificação do ID token do Google + getSession()
    sse.ts                 helper de Server-Sent Events
    useEventSource.ts       hook client-side pro SSE
    metrics.ts             porta de metrics_report.py::compute_report/funnel
    takedown-registry.ts    porta de registry.invoke_agent (valida contra o
                            input_schema do takedown-agent antes de publicar)
  proxy.ts                  guarda de autenticação (era middleware.ts)
  app/
    login/                 Sign In With Google
    (app)/                 tudo autenticado (Nav comum via layout.tsx)
      review/              fila + detalhe + Server Actions de decisão
      metrics/             token economy
      campaigns/           mapa de campanhas (MVP)
    api/
      session/             troca ID token <-> cookie
      artifact/             proxy autenticado do GCS
      stream/               os três SSE (queue/investigation/metrics)
```

## Critérios de aceite -- status

- ✅ Dockerfile + comando de deploy documentado acima.
- ✅ Aprovar grava no Firestore E publica em `takedown-approved` (validado
  contra o registry antes -- `src/lib/takedown-registry.ts`); testado
  contra o Firestore/registry reais neste sprint.
- ✅ Nenhum PII renderizado em tela (texto já sanitizado antes de chegar
  aqui; screenshot com blur condicional -- ver seção acima).
- ✅ Deployado e acessível: `https://sentinel-dashboard-433113110183.us-central1.run.app`
  (também resolve pela URL alternativa `https://sentinel-dashboard-cugvqtrd7q-uc.a.run.app`,
  mesma revisão). `infra/` aplicado de verdade (Terraform, 24 recursos --
  nunca tinha sido aplicado antes deste sprint), `dashboard-sa` rodando o
  serviço com permissão mínima.
