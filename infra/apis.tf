/**
 * Sprint 8, Parte B -- APIs habilitadas.
 *
 * `scripts/setup_gcp.sh` (gcloud) ja habilita pubsub/firestore/aiplatform/
 * telemetry hoje, e todas as 9 abaixo ja estao habilitadas no projeto real
 * usado neste sprint (confirmado via `gcloud services list --enabled`) --
 * mas nenhum Terraform as declarava ate agora. Declaramos aqui, todas,
 * para que "terraform apply + deploy.sh sobem o sistema completo do zero"
 * (criterio de aceite do sprint) seja verdade tambem num projeto GCP novo,
 * sem depender de rodar setup_gcp.sh antes.
 *
 * `disable_on_destroy = false` em TODAS -- runtime.googleapis.com,
 * pubsub.googleapis.com etc. sao recursos de PROJETO, compartilhados com o
 * dashboard (ja deployado, fora deste Terraform) e com qualquer coisa que
 * rodar manualmente fora daqui. Um `terraform destroy` deste modulo NUNCA
 * deve desabilitar uma API que outra coisa no projeto ainda usa -- ver
 * teardown.sh, que deliberadamente nao chama `terraform destroy`.
 */

locals {
  required_apis = [
    "run.googleapis.com",              # Cloud Run (services + jobs)
    "artifactregistry.googleapis.com", # imagens Docker do gateway/workers
    "cloudbuild.googleapis.com",       # build das imagens (deploy.sh)
    "pubsub.googleapis.com",           # ja habilitada por setup_gcp.sh, declarada aqui tambem
    "firestore.googleapis.com",        # idem
    "aiplatform.googleapis.com",       # idem -- Vertex AI (Gemini)
    "telemetry.googleapis.com",        # idem -- traces/metricas OTel
    "monitoring.googleapis.com",       # Cloud Monitoring (metricas + orcamento)
    "logging.googleapis.com",          # Cloud Logging
    "billingbudgets.googleapis.com",   # google_billing_budget (budget.tf) -- FALTAVA
    # nesta lista na primeira versao; nao era a causa do 403 real
    # observado neste sprint (ver nota de "quota project" em budget.tf/
    # deploy.sh), mas sem a API habilitada o orcamento falharia de
    # qualquer forma -- as duas causas precisam estar corrigidas juntas.
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
