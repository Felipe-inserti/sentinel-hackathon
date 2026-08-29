#!/usr/bin/env bash
#
# Sentinel - deploy.sh (Sprint 8, Parte B)
#
# Sobe o sistema completo do zero: habilita APIs, builda as 3 imagens
# Docker (agentes leves sem Playwright / evidence_agent+Playwright /
# orchestrator+Playwright -- Sprint multimodal, ver Dockerfile.orchestrator),
# aplica o Terraform (Agent Identity + Agent Gateway + Cloud Run Jobs dos 4
# workers), e IMPRIME (nunca executa sozinho) os comandos para iniciar os
# workers.
#
# Idempotente: pode rodar duas vezes seguidas sem quebrar -- cada etapa
# usa comandos que ja sao idempotentes por natureza (`gcloud builds
# submit`, `terraform apply`) ou tem uma checagem "ja existe, pulando"
# antes (mesmo padrao de scripts/setup_gcp.sh).
#
# NAO faz deploy do serving do Gemma (scripts/deploy_gemma_cloudrun.sh,
# separado -- dependencia circular real com a URL do proprio servico, ver
# aquele script) nem do dashboard (dashboard/README.md, ja deployado).
#
# Uso:
#   ./deploy.sh [PROJECT_ID] [REGION]
#
# Pre-requisitos:
#   - gcloud autenticado (`gcloud auth login` + `gcloud auth application-default login`)
#   - terraform >= 1.5 no PATH
#   - scripts/setup_gcp.sh ja rodou (topicos base, Firestore -- ver infra/README.md)
#   - `terraform apply` de infra/main.tf (Agent Identity, Sprint 3) ja rodou
#     pelo menos uma vez -- este script SO ADICIONA recursos novos (Sprint 8B)
#     sobre o state existente, nunca recria do zero.

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${2:-us-central1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY="sentinel-images"
AGENTS_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/sentinel-agents:latest"
EVIDENCE_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/sentinel-evidence:latest"
# Sprint multimodal -- orchestrator saiu de AGENTS_IMAGE (screenshot USADO
# na classificacao, ver plane2_agents/page_capture.py) para uma imagem
# propria com Playwright/Chromium, mesmo padrao de EVIDENCE_IMAGE. Ver
# Dockerfile.orchestrator e a docstring de var.orchestrator_image em
# infra/variables.tf.
ORCHESTRATOR_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/sentinel-orchestrator:latest"

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; }
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

if [[ -z "$PROJECT_ID" ]]; then
  err "Nenhum PROJECT_ID informado e nenhum projeto ativo no gcloud config."
  err "Uso: ./deploy.sh <PROJECT_ID> [REGION]"
  exit 1
fi

for bin in gcloud terraform; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "'$bin' nao encontrado no PATH."
    exit 1
  fi
done

cd "$REPO_ROOT"
log "Projeto alvo: ${PROJECT_ID} (regiao: ${REGION})"
gcloud config set project "$PROJECT_ID" >/dev/null

# --- 1. APIs base (idempotente -- gcloud services enable so nao-op se ja
#        habilitada). O Terraform tambem declara estas mesmas APIs como
#        recurso (infra/apis.tf) -- habilitar aqui primeiro evita que o
#        PRIMEIRO `terraform apply` de um projeto novo falhe tentando criar
#        recursos antes das APIs deles existirem.
#
#        Inclui billingbudgets.googleapis.com (infra/budget.tf) e o fix de
#        "quota project" das credenciais ADC locais -- incidente real deste
#        sprint: `terraform apply` do orcamento falhou com 403, o erro
#        citava um projeto CONSUMER diferente do projeto alvo (a Billing
#        Budgets API cobra a chamada contra o quota project da ADC local,
#        nao contra --project). `set-quota-project` e seguro rodar de novo
#        (idempotente) e nao tem efeito colateral fora de mudar essa
#        configuracao local do gcloud. ---
step "1/5 -- Habilitando APIs + quota project da ADC"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  telemetry.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  billingbudgets.googleapis.com \
  --project "$PROJECT_ID"

gcloud auth application-default set-quota-project "$PROJECT_ID"

# --- 2. Repositorio Artifact Registry (idempotente -- checa antes) ---
#        UNICO dono deste recurso -- o Terraform SO LE (data source, ver
#        infra/artifact_registry.tf), nunca cria. Corrigido neste sprint:
#        a versao anterior declarava isto como `resource` no Terraform
#        TAMBEM, e a segunda chamada de `terraform apply` batia 409
#        ALREADY_EXISTS (dois donos do mesmo recurso). Esta etapa PRECISA
#        rodar antes da etapa 3 (build de imagens, que faz push aqui) e
#        antes da etapa 4 (Terraform, que so LE o repositorio).
step "2/5 -- Artifact Registry"
if gcloud artifacts repositories describe "$REPOSITORY" \
    --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  warn "Repositorio '${REPOSITORY}' ja existe, pulando criacao."
else
  log "Criando repositorio '${REPOSITORY}' em ${REGION}..."
  gcloud artifacts repositories create "$REPOSITORY" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --repository-format=docker \
    --description="Imagens do agent-gateway e dos workers Python"
fi

# --- 3. Build das 3 imagens (Cloud Build -- sem necessidade de Docker local) ---
step "3/5 -- Build das imagens (Cloud Build, sem Docker local)"
log "Imagem 1/3: ct-listener + takedown-agent + agent-gateway -> ${AGENTS_IMAGE}"
# --tag e --config sao MUTUAMENTE EXCLUSIVOS em 'gcloud builds submit'
# ("At most one of --config | --pack | --tag can be specified", erro real
# reproduzido ao rodar este script) -- a tag ja vem do campo `images:` do
# YAML abaixo, entao --tag e redundante, nao complementar. Mesmo padrao
# de dashboard/cloudbuild.yaml (tambem sem --tag na chamada).
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config /dev/stdin <<EOF
steps:
  - name: "gcr.io/cloud-builders/docker"
    args: ["build", "-f", "Dockerfile", "-t", "${AGENTS_IMAGE}", "."]
images:
  - "${AGENTS_IMAGE}"
options:
  logging: CLOUD_LOGGING_ONLY
EOF

log "Imagem 2/3: evidence_agent (Playwright/Chromium) -> ${EVIDENCE_IMAGE}"
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config /dev/stdin <<EOF
steps:
  - name: "gcr.io/cloud-builders/docker"
    args: ["build", "-f", "Dockerfile.evidence", "-t", "${EVIDENCE_IMAGE}", "."]
images:
  - "${EVIDENCE_IMAGE}"
options:
  logging: CLOUD_LOGGING_ONLY
EOF

log "Imagem 3/3: orchestrator (Playwright/Chromium, Sprint multimodal) -> ${ORCHESTRATOR_IMAGE}"
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config /dev/stdin <<EOF
steps:
  - name: "gcr.io/cloud-builders/docker"
    args: ["build", "-f", "Dockerfile.orchestrator", "-t", "${ORCHESTRATOR_IMAGE}", "."]
images:
  - "${ORCHESTRATOR_IMAGE}"
options:
  logging: CLOUD_LOGGING_ONLY
EOF

# --- 4. Terraform: plan -> confirmacao humana -> apply ---
step "4/5 -- Terraform (infra/)"
cd "$REPO_ROOT/infra"
terraform init -upgrade=false

TF_VARS=(
  -var="project_id=${PROJECT_ID}"
  -var="region=${REGION}"
  -var="agents_image=${AGENTS_IMAGE}"
  -var="evidence_image=${EVIDENCE_IMAGE}"
  -var="orchestrator_image=${ORCHESTRATOR_IMAGE}"
)

log "Gerando plano..."
terraform plan "${TF_VARS[@]}" -out=tfplan

echo
warn "Revise o plano ACIMA com cuidado antes de confirmar."
read -r -p "Aplicar este plano? [y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  err "Cancelado pelo usuario -- nada foi aplicado."
  rm -f tfplan
  exit 1
fi

terraform apply tfplan
rm -f tfplan
cd "$REPO_ROOT"

# --- 5. Resumo + comandos de demo (NUNCA executados automaticamente) ---
step "5/5 -- Pronto. Nada foi iniciado ainda."
GATEWAY_URL="$(cd infra && terraform output -raw gateway_url 2>/dev/null || true)"

cat <<EOF

Infra aplicada. O agent-gateway esta deployado (min-instances=0, escala
sob demanda de verdade -- HTTP). Os 4 workers (ct-listener, orchestrator,
evidence-collector, takedown-agent) sao Cloud Run JOBS -- NAO rodam
sozinhos. Para a demo, inicie o que precisar com:

  gcloud run jobs execute ct-listener-job         --project ${PROJECT_ID} --region ${REGION} --async
  gcloud run jobs execute orchestrator-job        --project ${PROJECT_ID} --region ${REGION} --async
  gcloud run jobs execute evidence-collector-job  --project ${PROJECT_ID} --region ${REGION} --async
  gcloud run jobs execute takedown-agent-job      --project ${PROJECT_ID} --region ${REGION} --async

(--async devolve o prompt na hora -- os workers continuam rodando em
background ate o timeout de ${REPO_ROOT}/infra/variables.tf::job_task_timeout_seconds
ou ate voce rodar ./teardown.sh)

agent-gateway: ${GATEWAY_URL:-"(rode 'terraform output gateway_url' em infra/)"}

IMPORTANTE: rode ./teardown.sh depois da gravacao/avaliacao para garantir
custo zero (cancela qualquer execucao de Job ainda rodando).
EOF
