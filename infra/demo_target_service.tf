/**
 * Sprint 2, Stage D -- alvo publico de demo como Cloud Run Service, no
 * lugar do bucket GCS anterior (infra/demo_target_bucket.tf, REMOVIDO
 * neste mesmo commit -- ver FINDINGS.md item 17: `website{main_page_suffix}`
 * do GCS nao funciona sem Load Balancer + dominio proprio, e usar URL por
 * caminho como "domain" quebrou o Firestore e o domain-lock de
 * page_capture.py, ver FINDINGS.md itens 16/18, ambos corrigidos em
 * codigo -- mas a causa raiz continuava sendo "este alvo nao tem hostname
 * puro de verdade"). Um Cloud Run Service da isso de graca, sem dominio
 * proprio nenhum -- mesmo mecanismo que sentinel-agent-gateway ja usa
 * neste projeto.
 *
 * ADITIVO e REMOVIVEL: arquivo proprio, condicionado a
 * `var.demo_target_image != ""` (default vazio -- ver variables.tf) --
 * um `terraform apply` do sistema real, sem essa var, simplesmente NAO
 * cria nada aqui. Destruivel sozinho com
 * `terraform destroy -target=google_cloud_run_v2_service.demo_target
 * -target=google_cloud_run_v2_service_iam_member.demo_target_public_invoke
 * -target=google_service_account.demo_target`.
 *
 * Serve SO os 5 arquivos aprovados (ver Dockerfile.demo-target -- lista
 * exaustiva de COPY, nunca `COPY demo/phishing-target/ ./`).
 * `malicious_nofooter.html` nunca entra na imagem, em nenhuma hipotese.
 *
 * IAM minimo: `demo-target-sa` nao ganha NENHUM papel alem de rodar o
 * container -- o servidor (cloud_run_server.py) e stdlib puro, nao chama
 * Firestore/Pub-Sub/Vertex AI/nada, entao nao precisa de nenhum
 * `google_project_iam_member`. Publico (`allUsers:run.invoker`) de
 * proposito -- mesmo raciocinio do bucket anterior (paginas de demo com
 * marca ficticia e rodape de divulgacao, nunca dado real, ver
 * Dockerfile.demo-target).
 */

resource "google_service_account" "demo_target" {
  count        = var.demo_target_image != "" ? 1 : 0
  project      = var.project_id
  account_id   = "demo-target-sa"
  display_name = "Sentinel - alvo publico de demo (Stage D)"
  description  = "Servidor estatico minimo (stdlib) de demo/phishing-target/ -- nao chama nenhuma API do Google Cloud, sem papeis alem de rodar o container."
}

resource "google_cloud_run_v2_service" "demo_target" {
  count               = var.demo_target_image != "" ? 1 : 0
  project             = var.project_id
  name                = "sentinel-demo-target"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.demo_target[0].email

    # Escala a zero entre usos -- so paga quando alguem (ou o
    # orchestrator) de fato acessa. Trafego baixo/esporadico, sem SLA de
    # latencia (nao e o sistema real).
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.demo_target_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          # 256Mi rejeitado pelo Cloud Run em runtime (nao no plan --
          # erro real reproduzido no apply): "Total memory < 512 Mi is
          # not supported with cpu always allocated (unthrottled)". 512Mi
          # e o piso, mesmo para um `http.server` stdlib que mal usa
          # memoria nenhuma.
          memory = "512Mi"
        }
      }

      # Qual dos 5 arquivos aparece em GET / -- trocavel sem rebuild via
      # `gcloud run services update sentinel-demo-target
      # --update-env-vars=SERVE_AS_ROOT=<arquivo>.html` (mesma imagem,
      # mesmo digest, so a env var muda -- redeploy rapido). Default
      # malicious.html (o caso MALICIOUS principal do Stage D.2).
      env {
        name  = "SERVE_AS_ROOT"
        value = "malicious.html"
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}

resource "google_cloud_run_v2_service_iam_member" "demo_target_public_invoke" {
  count    = var.demo_target_image != "" ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.demo_target[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "demo_target_service_url" {
  value       = var.demo_target_image != "" ? google_cloud_run_v2_service.demo_target[0].uri : null
  description = "URL publica do alvo de demo (hostname puro, sem path -- passe SO o hostname, sem 'https://', como 'domain' pro orchestrator)."
}
