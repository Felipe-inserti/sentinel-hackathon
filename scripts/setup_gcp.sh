#!/usr/bin/env bash
#
# Sentinel - Infrastructure as Code (bootstrap GCP)
#
# Cria os topicos Pub/Sub e a subscription necessarios para o pipeline
# Plano 1 (ingestao) -> Plano 2 (orquestrador de agentes), alem de garantir
# que as APIs necessarias estejam habilitadas e que exista um banco
# Firestore no projeto.
#
# Uso:
#   ./scripts/setup_gcp.sh [PROJECT_ID] [REGION]
#
# Se PROJECT_ID nao for passado, usa o projeto ativo do `gcloud config`.

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${2:-us-central1}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"

TOPIC_SUSPICIOUS="suspicious-domain-detected"
TOPIC_COMPLETED="investigation-completed"
SUBSCRIPTION_ORCHESTRATOR="sub-orchestrator"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }

if [[ -z "$PROJECT_ID" ]]; then
  err "Nenhum PROJECT_ID informado e nenhum projeto ativo no gcloud config."
  err "Uso: ./scripts/setup_gcp.sh <PROJECT_ID> [REGION]"
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  err "gcloud CLI nao encontrado no PATH. Instale o Google Cloud SDK primeiro."
  exit 1
fi

log "Projeto alvo: ${PROJECT_ID} (regiao: ${REGION})"
gcloud config set project "$PROJECT_ID" >/dev/null

log "Habilitando APIs necessarias (pubsub, firestore, vertex ai, telemetry)..."
gcloud services enable \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  telemetry.googleapis.com \
  --project "$PROJECT_ID"

create_topic_if_missing() {
  local topic_id="$1"
  if gcloud pubsub topics describe "$topic_id" --project "$PROJECT_ID" >/dev/null 2>&1; then
    warn "Topico '${topic_id}' ja existe, pulando."
  else
    log "Criando topico '${topic_id}'..."
    gcloud pubsub topics create "$topic_id" --project "$PROJECT_ID"
  fi
}

create_topic_if_missing "$TOPIC_SUSPICIOUS"
create_topic_if_missing "$TOPIC_COMPLETED"

if gcloud pubsub subscriptions describe "$SUBSCRIPTION_ORCHESTRATOR" --project "$PROJECT_ID" >/dev/null 2>&1; then
  warn "Subscription '${SUBSCRIPTION_ORCHESTRATOR}' ja existe, pulando."
else
  log "Criando subscription '${SUBSCRIPTION_ORCHESTRATOR}' (consome de '${TOPIC_SUSPICIOUS}')..."
  gcloud pubsub subscriptions create "$SUBSCRIPTION_ORCHESTRATOR" \
    --topic "$TOPIC_SUSPICIOUS" \
    --project "$PROJECT_ID" \
    --ack-deadline=60 \
    --message-retention-duration=1d \
    --min-retry-delay=10s \
    --max-retry-delay=60s
fi

# Firestore precisa de um database no modo Native. Se o projeto ja tiver um
# banco (ex: "(default)"), o comando abaixo falha de forma idempotente --
# tratamos isso como aviso, nao como erro fatal.
log "Verificando banco Firestore (modo Native)..."
if gcloud firestore databases describe --database="(default)" --project "$PROJECT_ID" >/dev/null 2>&1; then
  warn "Banco Firestore '(default)' ja existe, pulando criacao."
else
  log "Criando banco Firestore '(default)' em modo Native (location=${FIRESTORE_LOCATION})..."
  gcloud firestore databases create \
    --database="(default)" \
    --location="$FIRESTORE_LOCATION" \
    --type=firestore-native \
    --project "$PROJECT_ID"
fi

log "Recursos provisionados com sucesso:"
log "  - Pub/Sub topic:        ${TOPIC_SUSPICIOUS}"
log "  - Pub/Sub topic:        ${TOPIC_COMPLETED}"
log "  - Pub/Sub subscription: ${SUBSCRIPTION_ORCHESTRATOR} -> ${TOPIC_SUSPICIOUS}"
log "  - Firestore database:   (default) [${FIRESTORE_LOCATION}]"
log "  - API habilitada:       aiplatform.googleapis.com (Vertex AI / Gemini)"
log "  - API habilitada:       telemetry.googleapis.com (traces/metricas OTel, ver telemetry.py)"
log ""
log "Este script NAO cria service accounts nem concede papeis IAM -- rode"
log "isso manualmente (ou via Terraform) para a identidade que vai executar"
log "ct_listener.py e orchestrator.py. Papeis minimos necessarios:"
log "  - roles/aiplatform.user      (chamar o Gemini via Vertex AI)"
log "  - roles/datastore.user       (ler/escrever no Firestore, incl. metricas)"
log "  - roles/pubsub.publisher     (publicar nos topicos acima)"
log "  - roles/pubsub.subscriber    (consumir de ${SUBSCRIPTION_ORCHESTRATOR})"
log "  - roles/telemetry.writer     (exportar traces+metricas via Telemetry API --"
log "                                 ou os 3 papeis granulares: telemetry.tracesWriter,"
log "                                 monitoring.metricWriter e logging.logWriter)"
log ""
log "Sem os papeis de telemetry/monitoring, o pipeline funciona normalmente --"
log "telemetry.py falha aberto (loga e segue) se a exportacao for negada;"
log "so o Cloud Trace/Monitoring ficam vazios. OTEL_ENABLED=false desliga a"
log "exportacao de vez (uso local/CI)."
log ""
log "Proximo passo: export GCP_PROJECT_ID=${PROJECT_ID} antes de rodar os planos 1 e 2."
