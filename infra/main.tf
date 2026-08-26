/**
 * Sentinel -- Agent Identity (Parte B do sprint de Agent Registry/Identity).
 *
 * Cria uma Service Account por agente com permissao minima, seguindo o
 * modelo zero-trust exigido pela trilha: cada agente so pode fazer
 * exatamente o que seu papel exige, nada por "conveniencia". O caso mais
 * importante e `takedown-sa` (ver comentario no bloco de IAM dela): a
 * garantia de "nenhum takedown sem aprovacao humana" e TOPOLOGICA (so
 * consegue consumir uma aprovacao que `dashboard-sa` ja publicou), nao
 * "zero permissao" -- desde que `takedown_agent.py` (Sprint 6) precisa
 * reconfirmar a aprovacao no Firestore e chamar o Gemini para decidir
 * canais, `takedown-sa` teve que ganhar `roles/datastore.user` e
 * `roles/aiplatform.user`. A restricao de "so LEITURA em `investigations`"
 * que isso deixou de garantir por IAM (Firestore nao tem IAM por colecao,
 * ver nota ¹ do README) e imposta em codigo, nao aqui -- ver
 * `takedown_agent.py::ReadOnlyCollectionAccess`.
 *
 * Este Terraform NAO duplica a infraestrutura ja criada por
 * `scripts/setup_gcp.sh` (topicos `suspicious-domain-detected`/
 * `investigation-completed`, subscription `sub-orchestrator`, banco
 * Firestore) -- esses recursos continuam sendo responsabilidade daquele
 * script e sao so REFERENCIADOS aqui pelo nome nos IAM bindings, para
 * evitar dois donos (Terraform + gcloud) do mesmo recurso. A infraestrutura
 * nova de fato criada aqui: o topico/subscription de aprovacao de takedown
 * (`takedown-approved`/`sub-takedown` -- CLAUDE.md ja documentava
 * `takedown-approved` como topico "existente", mas nenhum script do
 * projeto o criava de verdade ate agora), a subscription `sub-evidence`
 * sobre o topico `investigation-completed` ja existente (evidence-sa
 * consome so o que classification == MALICIOUS, filtro em codigo -- ver
 * evidence_agent.py), e o bucket de evidencia -- nenhum dos tres com dono
 * anterior.
 */

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  evidence_bucket_name = coalesce(var.evidence_bucket_name, "${var.project_id}-sentinel-evidence")
}

# ---------------------------------------------------------------------------
# Pub/Sub -- topico + subscription de aprovacao de takedown (novos, ver
# docstring acima).
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "takedown_approved" {
  project = var.project_id
  name    = var.takedown_topic_id
}

resource "google_pubsub_subscription" "sub_takedown" {
  project = var.project_id
  name    = var.takedown_subscription_id
  topic   = google_pubsub_topic.takedown_approved.id

  # Mesmos valores usados para sub-orchestrator em scripts/setup_gcp.sh --
  # mantem os dois pipelines de aprovacao/investigacao com o mesmo
  # comportamento de retry/retencao.
  ack_deadline_seconds       = 60
  message_retention_duration = "86400s" # 1 dia

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }
}

# Subscription de evidence-sa sobre o topico investigation-completed JA
# EXISTENTE (criado por scripts/setup_gcp.sh, mesmo topico que
# orchestrator-sa publica) -- so a subscription e nova, referenciada por
# nome (var.completed_topic_id), nao por resource Terraform, mesma logica
# de nao duplicar dono usada no resto do arquivo.
resource "google_pubsub_subscription" "sub_evidence" {
  project = var.project_id
  name    = var.evidence_subscription_id
  topic   = var.completed_topic_id

  ack_deadline_seconds       = 60
  message_retention_duration = "86400s" # 1 dia

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "60s"
  }
}

# ---------------------------------------------------------------------------
# Cloud Storage -- bucket de evidencia (evidence-sa). Sem dono anterior.
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "evidence" {
  project                     = var.project_id
  name                        = local.evidence_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
}

# ---------------------------------------------------------------------------
# Service Accounts -- uma por agente (Agent Identity).
# ---------------------------------------------------------------------------

resource "google_service_account" "ct_listener" {
  project      = var.project_id
  account_id   = "ct-listener-sa"
  display_name = "Sentinel - ct-listener"
  description  = "Ingestao CT. Permissao minima: so publica em ${var.suspicious_topic_id}."
}

resource "google_service_account" "orchestrator" {
  project      = var.project_id
  account_id   = "orchestrator-sa"
  display_name = "Sentinel - orchestrator"
  description  = "Investigacao (scraping + Gemini). Subscribe em ${var.orchestrator_subscription_id}, Firestore, Vertex AI, publish em ${var.completed_topic_id}."
}

resource "google_service_account" "evidence" {
  project      = var.project_id
  account_id   = "evidence-sa"
  display_name = "Sentinel - evidence-collector"
  description  = "Coleta de evidencia. Subscribe em ${var.evidence_subscription_id}, write em Cloud Storage (${local.evidence_bucket_name}), read/write Firestore."
}

resource "google_service_account" "takedown" {
  project      = var.project_id
  account_id   = "takedown-sa"
  display_name = "Sentinel - takedown-agent"
  description  = "Consome ${var.takedown_subscription_id} (unico publisher do topico e dashboard-sa), reconfirma aprovacao e audita em Firestore, decide canais/redige notificacao com Gemini. So-leitura em investigations e garantia de aplicacao, nao de IAM -- ver README."
}

resource "google_service_account" "dashboard" {
  project      = var.project_id
  account_id   = "dashboard-sa"
  display_name = "Sentinel - dashboard"
  description  = "Leitura/gravacao de Firestore (fila de revisao humana grava approved_by/approved_at/decision_rationale), leitura do bucket de evidencia, publica aprovacoes de takedown em ${var.takedown_topic_id}."
}

# ---------------------------------------------------------------------------
# IAM -- ct-listener-sa: so publish no topico de dominios suspeitos.
# ---------------------------------------------------------------------------

resource "google_pubsub_topic_iam_member" "ct_listener_publish_suspicious" {
  project = var.project_id
  topic   = var.suspicious_topic_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.ct_listener.email}"
}

# ---------------------------------------------------------------------------
# IAM -- orchestrator-sa: subscribe na investigacao, Firestore, Vertex AI,
# publish na conclusao.
# ---------------------------------------------------------------------------

resource "google_pubsub_subscription_iam_member" "orchestrator_subscribe" {
  project      = var.project_id
  subscription = var.orchestrator_subscription_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_pubsub_topic_iam_member" "orchestrator_publish_completed" {
  project = var.project_id
  topic   = var.completed_topic_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

# roles/datastore.user e roles/aiplatform.user sao papeis de PROJETO --
# Firestore/Vertex AI nao oferecem IAM por colecao/recurso individual sem
# regras de seguranca customizadas adicionais (fora do escopo deste
# sprint). Documentado explicitamente na matriz do README para nao passar
# a impressao de um isolamento mais fino do que o que existe de fato.
resource "google_project_iam_member" "orchestrator_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_project_iam_member" "orchestrator_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

# ---------------------------------------------------------------------------
# IAM -- evidence-sa: consumir investigation-completed (so MALICIOUS, em
# codigo), write no bucket de evidencia, Firestore.
# ---------------------------------------------------------------------------

resource "google_pubsub_subscription_iam_member" "evidence_subscribe" {
  project      = var.project_id
  subscription = google_pubsub_subscription.sub_evidence.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.evidence.email}"
}

resource "google_storage_bucket_iam_member" "evidence_write" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.evidence.email}"
}

resource "google_project_iam_member" "evidence_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.evidence.email}"
}

# ---------------------------------------------------------------------------
# IAM -- takedown-sa: consumir sub-takedown (nenhum roles/pubsub.publisher
# em NADA -- este agente nunca publica), Firestore (reconfirmar aprovacao,
# gravar auditoria/rate limit) e Vertex AI (Gemini decide canais/redige
# notificacao). A garantia de "nenhum takedown sem aprovacao humana
# registrada" (regra do CLAUDE.md) e TOPOLOGICA, nao de isolamento total:
# sub-takedown so recebe mensagens que dashboard-sa publicou em
# takedown-approved (nenhuma outra identidade tem roles/pubsub.publisher
# nesse topico) -- ver README, secao "Por que takedown-sa e a peca central".
#
# roles/datastore.user e de PROJETO (mesma limitacao ja documentada para
# orchestrator-sa/evidence-sa, nota ¹ do README): tecnicamente permite
# escrever em `investigations` tambem, nao so em takedown_actions/
# takedown_rate_limits. Isso e um risco conhecido e aceito, mitigado em
# CODIGO -- takedown_agent.py so acessa `investigations` atraves de
# `ReadOnlyCollectionAccess`, que nao expoe nenhum metodo de escrita.
# ---------------------------------------------------------------------------

resource "google_pubsub_subscription_iam_member" "takedown_subscribe_only" {
  project      = var.project_id
  subscription = google_pubsub_subscription.sub_takedown.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.takedown.email}"
}

resource "google_project_iam_member" "takedown_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.takedown.email}"
}

resource "google_project_iam_member" "takedown_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.takedown.email}"
}

# ---------------------------------------------------------------------------
# IAM -- dashboard-sa: leitura/gravacao de Firestore (o dashboard, Sprint 5,
# grava approved_by/approved_at/decision_rationale/rejection_reason no
# proprio documento de investigacao -- roles/datastore.viewer sozinho, que
# bastava so pra leitura, deixou de ser suficiente), leitura do bucket de
# evidencia (proxy de screenshot/HTML, nunca escreve la -- so evidence-sa
# escreve), publish da aprovacao humana.
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "dashboard_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}

resource "google_storage_bucket_iam_member" "dashboard_read_evidence" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.dashboard.email}"
}

resource "google_pubsub_topic_iam_member" "dashboard_publish_takedown_approved" {
  project = var.project_id
  topic   = google_pubsub_topic.takedown_approved.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}

# ---------------------------------------------------------------------------
# IAM -- Observabilidade (Cloud Trace + Cloud Monitoring), as 5 SAs deste
# arquivo. Achado real da sessao de validacao de 48h (Sprint 8): o pipeline
# funciona ponta a ponta (inclusive propagacao de trace_id pelo Pub/Sub,
# codigo correto em telemetry.py) e `estimated_cost_saved_usd_total`/etc.
# continuam gravando em Firestore normalmente -- mas NENHUMA das 5 SAs tinha
# `roles/cloudtrace.agent`/`roles/monitoring.metricWriter`, entao toda
# chamada de exportacao (`telemetry._try_build_span_processor`/
# `_try_build_metric_reader`) falhava com PERMISSION_DENIED em
# `telemetry.traces.write`/`monitoring.timeSeries.create` -- Cloud Trace e
# Cloud Monitoring ficavam cegos (best-effort, ver telemetry.py: o erro e
# logado e o processo segue, entao isso nao aparecia como falha dura em
# lugar nenhum ate a inspecao manual).
#
# So 4 processos chamam `telemetry.setup()` hoje (ct_listener.py/
# orchestrator.py/evidence_agent.py/takedown_agent.py -- ver grep contra o
# repo) -- `dashboard-sa` (Next.js, sem instrumentacao OTel neste sprint)
# NAO emite telemetria ainda. Incluida aqui mesmo assim por pedido explicito
# ("as 5 Service Accounts") e para manter a mesma cobertura das 5 SAs deste
# arquivo sem exigir voltar aqui se o dashboard ganhar OTel depois -- o
# custo de conceder um papel de projeto nao usado e zero (sem escrita
# nenhuma acontece sem chamada correspondente no codigo). `gateway-sa`
# (cloud_run_gateway.tf) fica DE FORA de proposito: `agent_gateway.py` (Sprint
# 8 Parte A) tambem nao chama `telemetry.setup()` ainda -- mesma lacuna,
# fora do escopo desta correcao pontual.
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "ct_listener_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.ct_listener.email}"
}

resource "google_project_iam_member" "ct_listener_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.ct_listener.email}"
}

resource "google_project_iam_member" "orchestrator_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_project_iam_member" "orchestrator_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_project_iam_member" "evidence_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.evidence.email}"
}

resource "google_project_iam_member" "evidence_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.evidence.email}"
}

resource "google_project_iam_member" "takedown_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.takedown.email}"
}

resource "google_project_iam_member" "takedown_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.takedown.email}"
}

resource "google_project_iam_member" "dashboard_trace" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}

resource "google_project_iam_member" "dashboard_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}
