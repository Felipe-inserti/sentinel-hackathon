#!/usr/bin/env bash
#
# Sentinel - deploy do serving do Gemma 3 270M (Ollama) no Cloud Run.
#
# Pendencia do Sprint 2.5 ("adiada"), resolvida no Sprint 8 Parte B.
# Referenciado por `config.py::Settings.gemma_ollama_base_url` desde
# aquele sprint -- ate agora nenhum script realmente fazia este deploy.
#
# Como os outros 4 processos deste repo (ver infra/), este e um script
# gcloud simples, NAO gerenciado por Terraform -- mesma logica ja usada
# por dashboard/README.md (a build/deploy do dashboard tambem e via
# `gcloud builds submit`/`gcloud run deploy`, nao Terraform): o dono deste
# Cloud Run Service e este script, nao infra/.
#
# Idempotente: `gcloud run deploy` sobre um servico existente atualiza a
# revisao em vez de falhar.
#
# Uso:
#   ./scripts/deploy_gemma_cloudrun.sh [PROJECT_ID] [REGION]
#
# Pre-requisito: `terraform apply` em infra/ ja rodou (cria o repositorio
# de Artifact Registry `sentinel-images` que este script reusa -- ver
# infra/artifact_registry.tf).

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${2:-us-central1}"
SERVICE_NAME="sentinel-gemma-triage"
REPOSITORY="sentinel-images"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/gemma-triage:latest"

log()  { printf '\033[1;34m[gemma]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[gemma]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[gemma]\033[0m %s\n' "$*" >&2; }

if [[ -z "$PROJECT_ID" ]]; then
  err "Nenhum PROJECT_ID informado e nenhum projeto ativo no gcloud config."
  err "Uso: ./scripts/deploy_gemma_cloudrun.sh <PROJECT_ID> [REGION]"
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  err "gcloud CLI nao encontrado no PATH."
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! gcloud artifacts repositories describe "$REPOSITORY" \
    --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  err "Repositorio Artifact Registry '${REPOSITORY}' nao existe em ${REGION}."
  err "Rode 'terraform apply' em infra/ primeiro (ver infra/README.md)."
  exit 1
fi

log "Buildando ${IMAGE} a partir de Dockerfile.gemma (isso baixa o Gemma 3 270M na build -- pode levar alguns minutos)..."
# --tag e --config sao mutuamente exclusivos em 'gcloud builds submit' --
# ver comentario identico em deploy.sh. A tag ja vem do `images:` abaixo.
gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config /dev/stdin <<EOF
steps:
  - name: "gcr.io/cloud-builders/docker"
    args: ["build", "-f", "Dockerfile.gemma", "-t", "${IMAGE}", "."]
images:
  - "${IMAGE}"
options:
  logging: CLOUD_LOGGING_ONLY
EOF

log "Deployando ${SERVICE_NAME} em ${REGION}..."
# --ingress=internal (NAO --allow-unauthenticated sem isso seria publico
# na internet inteira): restringe a rede -- so outros recursos do MESMO
# projeto GCP (incl. os Cloud Run Jobs deste repo, ver infra/) alcancam a
# porta 8080. gemma_triage.py (ver seu HTTP client) nao envia nenhum
# cabecalho de autenticacao hoje -- adicionar isso seria mudanca de
# codigo de aplicacao, fora do escopo deste sprint (aditivo/deploy
# apenas). --ingress=internal cobre o gap sem tocar em codigo: o
# endpoint fica inalcancavel da internet publica, so nao tem uma segunda
# camada de identidade por chamada (mesmo tipo de decisao documentada
# para gateway-sa/dashboard-sa em infra/README.md -- aqui, risco residual
# aceito e documentado, nao escondido).
gcloud run deploy "$SERVICE_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --port 8080 \
  --cpu 2 \
  --memory 2Gi \
  --min-instances 0 \
  --max-instances 2 \
  --timeout 300 \
  --ingress internal \
  --allow-unauthenticated \
  --set-env-vars "OLLAMA_HOST=0.0.0.0:8080,OLLAMA_KEEP_ALIVE=-1"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --project "$PROJECT_ID" --region "$REGION" --format="value(status.url)")"

log "Deploy concluido: ${SERVICE_URL}"
warn "Configure GEMMA_OLLAMA_BASE_URL=${SERVICE_URL} no Job de ct-listener"
warn "(unico consumidor de gemma_triage.py) -- este script NAO atualiza o"
warn "Terraform sozinho, e uma dependencia circular real: o Job so pode"
warn "apontar pra esta URL depois que ela existe. Rode:"
warn ""
warn "  terraform apply -var=\"project_id=${PROJECT_ID}\" -var=\"gemma_ollama_base_url=${SERVICE_URL}\""
warn ""
warn "(ver variavel gemma_ollama_base_url em infra/variables.tf, default vazio"
warn "= gemma_triage.py cai no fail-open direto, sem tentar chamar nada)."
warn ""
warn "--ingress=internal: este endpoint NAO e alcancavel da internet publica,"
warn "so por outros recursos do projeto ${PROJECT_ID}. gemma_triage.py cai em"
warn "fail-open (ver config.py) se a chamada falhar -- nunca bloqueia o"
warn "pipeline principal por causa deste servico estar fora do ar."
