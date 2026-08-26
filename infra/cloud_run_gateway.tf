/**
 * Sprint 8, Parte B -- Agent Gateway (agent_gateway.py, Sprint 8 Parte A)
 * como Cloud Run Service, com sua propria Service Account de permissao
 * minima -- mesmo modelo zero-trust de `main.tf` (uma SA por agente).
 *
 * Por que Service (nao Job, como os 4 workers em cloud_run_jobs.tf): e o
 * UNICO dos processos novos deste sprint que serve HTTP de verdade (os
 * outros sao pull loops sem servidor nenhum) -- se encaixa no modelo
 * request-driven do Cloud Run Service e PODE escalar a zero de verdade
 * entre chamadas, sem o problema de "quem aciona" que os workers tem (ver
 * infra/README.md).
 *
 * IAM de gateway-sa -- deliberadamente SEM `roles/pubsub.publisher` no
 * topico `takedown-approved`: decisao arquitetural do Sprint 8 Parte A
 * (ver agent_gateway.py::AUTHORIZATION_POLICY e a secao "Decisao -- o
 * Agent Gateway nunca ganha publish em takedown-approved" mais abaixo
 * neste README/infra). `dashboard-sa` continua a UNICA identidade com
 * publish nesse topico -- a garantia topologica de "nenhum takedown sem
 * aprovacao humana" (regra #4 do CLAUDE.md) permanece intacta.
 */

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_service_account" "gateway" {
  project      = var.project_id
  account_id   = "gateway-sa"
  display_name = "Sentinel - agent-gateway"
  description  = "Ponto unico de entrada HTTP p/ invocar agentes (agent_gateway.py). Publica so em suspicious-domain-detected/investigation-completed. NUNCA em takedown-approved -- ver infra/README.md."
}

# Roteamento de orchestrator/evidence-collector (ver agent_gateway.py::
# AGENT_ROUTING_TOPIC) -- os dois UNICOS topicos onde gateway-sa publica.
resource "google_pubsub_topic_iam_member" "gateway_publish_suspicious" {
  project = var.project_id
  topic   = var.suspicious_topic_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_pubsub_topic_iam_member" "gateway_publish_completed" {
  project = var.project_id
  topic   = var.completed_topic_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

# roles/datastore.user e de PROJETO (mesma limitacao ja documentada para
# as outras 5 SAs, nota 1 do README) -- usado para: resolver o registry
# (leitura), gravar agent_gateway_audit_log (toda chamada) e
# agent_gateway_rate_limits (contador transacional). gateway-sa NAO ganha
# roles/aiplatform.user -- o gateway nunca chama o Gemini diretamente.
resource "google_project_iam_member" "gateway_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_cloud_run_v2_service" "gateway" {
  project  = var.project_id
  name     = "sentinel-agent-gateway"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  # false explicito -- ver comentario extenso em cloud_run_jobs.tf sobre
  # o incidente real que o default implicito (true) causou neste sprint.
  # Mesmo raciocinio aqui: recriavel do zero via deploy.sh, sem estado
  # proprio que valha proteger contra destroy.
  deletion_protection = false

  template {
    service_account = google_service_account.gateway.email

    scaling {
      min_instance_count = var.gateway_min_instance_count
      max_instance_count = var.gateway_max_instance_count
    }

    containers {
      image = var.agents_image
      # Dockerfile compartilhado (ver raiz do repo) usa este CMD como
      # default -- explicito aqui mesmo assim, documentacao > implicito.
      command = ["uvicorn", "agent_gateway:app", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_LOCATION"
        value = var.gcp_location_for_vertex
      }
      env {
        name  = "GEMINI_MODEL_ID"
        value = var.gemini_model_id
      }
      env {
        name  = "OTEL_REGION"
        value = var.otel_region
      }
      # URL PREVISIVEL do proprio servico -- Cloud Run v2 usa o formato
      # "https://{name}-{project_number}.{region}.run.app" (confirmado
      # contra o dashboard ja deployado neste projeto:
      # sentinel-dashboard-433113110183.us-central1.run.app segue
      # exatamente este padrao). Calculado ANTES da criacao do recurso --
      # sem isso seria uma referencia circular (o env var dependeria do
      # proprio `.uri` do recurso que o esta declarando).
      env {
        name  = "AGENT_GATEWAY_AUDIENCE"
        value = "https://sentinel-agent-gateway-${data.google_project.this.number}.${var.region}.run.app"
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}

# --allow-unauthenticated (roles/run.invoker para allUsers) -- MESMO
# padrao ja usado pelo dashboard (ver dashboard/README.md): o Cloud Run
# aceitar a requisicao HTTP sem IAM de plataforma nao e a autenticacao de
# verdade -- quem pode USAR o gateway e decidido pelo proprio
# agent_gateway.py (verificacao de ID token do Google + AUTHORIZATION_POLICY,
# ver Sprint 8 Parte A), nao pelo IAM do Cloud Run.
resource "google_cloud_run_v2_service_iam_member" "gateway_public_invoke" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
