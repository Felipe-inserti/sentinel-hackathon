/**
 * Sprint 8, Parte B -- os 4 workers de longa duracao (ct_listener,
 * orchestrator, evidence_agent, takedown_agent) como Cloud Run Jobs,
 * executados SOB DEMANDA (nao Services, nao Scheduler) -- decisao
 * explicada em detalhe em infra/README.md, secao "Por que Jobs sob
 * demanda, nao Services".
 *
 * Resumo: os 4 sao processos "conecte e rode pra sempre" (websocket ou
 * `subscribe()` streaming do Pub/Sub) SEM nenhum servidor HTTP -- nao se
 * encaixam no modelo request-driven de Cloud Run Service (que so escala
 * quando chega uma requisicao). Cloud Run Job e o primitivo certo pra um
 * processo de longa duracao sem HTTP: cobra SO enquanto uma execucao esta
 * rodando (nada em repouso, confirmado contra a documentacao oficial
 * nesta sessao) -- voce inicia com `gcloud run jobs execute --async`
 * antes da demo (ver deploy.sh/scripts/demo_up.sh) e cancela depois (ver
 * teardown.sh). `job_task_timeout_seconds` (default 3h) e o teto de
 * "esqueci de cancelar".
 *
 * Reusa as SAs de main.tf (ct-listener-sa/orchestrator-sa/evidence-sa/
 * takedown-sa) -- ja tem exatamente a permissao que cada Job precisa,
 * nenhuma IAM nova aqui.
 *
 * `deletion_protection = false` explicito nos 4 -- o provider Google
 * default esse campo pra `true` em `google_cloud_run_v2_job`/`_service`
 * quando nao declarado (bloqueia DESTROY, inclusive o destroy+recreate
 * que uma troca de `image` exige neste recurso -- nao e um update in-place).
 * Incidente real neste sprint: o default implicito travou exatamente
 * essa recriacao com "cannot destroy job without setting
 * deletion_protection=false". Decisao deliberada: um projeto de
 * hackathon que precisa ser derrubado por completo depois do
 * julgamento (ver teardown.sh) nao deve ter essa protecao ligada por
 * omissao -- os workers sao 100% recriaveis via `deploy.sh` a partir do
 * codigo/imagens versionados, nao ha estado irreplicavel neles (ao
 * contrario de `google_storage_bucket.evidence`/Firestore, que este
 * Terraform nunca tenta apagar).
 */

resource "google_cloud_run_v2_job" "ct_listener" {
  project             = var.project_id
  name                = "ct-listener-job"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.ct_listener.email
      timeout         = "${var.job_task_timeout_seconds}s"
      max_retries     = var.job_max_retries

      containers {
        image   = var.agents_image
        command = ["python"]
        args    = ["-m", "plane1_ingestion.ct_listener"]

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

        # So injetado quando o servico Gemma ja foi deployado (ver
        # scripts/deploy_gemma_cloudrun.sh e a variavel acima) --
        # dependencia circular real: a URL nao existe no primeiro apply.
        dynamic "env" {
          for_each = var.gemma_ollama_base_url != "" ? [var.gemma_ollama_base_url] : []
          content {
            name  = "GEMMA_OLLAMA_BASE_URL"
            value = env.value
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}

resource "google_cloud_run_v2_job" "orchestrator" {
  project             = var.project_id
  name                = "orchestrator-job"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.orchestrator.email
      timeout         = "${var.job_task_timeout_seconds}s"
      max_retries     = var.job_max_retries

      containers {
        image   = var.agents_image
        command = ["python"]
        args    = ["-m", "plane2_agents.orchestrator"]

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
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
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}

resource "google_cloud_run_v2_job" "evidence_collector" {
  project             = var.project_id
  name                = "evidence-collector-job"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.evidence.email
      timeout         = "${var.job_task_timeout_seconds}s"
      max_retries     = var.job_max_retries

      containers {
        # Imagem SEPARADA (Dockerfile.evidence) -- Playwright/Chromium,
        # mais pesada. Ver infra/README.md.
        image   = var.evidence_image
        command = ["python"]
        args    = ["evidence_agent.py"]

        resources {
          # Chromium headless e memory-hungry -- 2Gi de margem (os outros
          # 3 workers, sem browser, ficam em 512Mi-1Gi).
          limits = {
            cpu    = "2"
            memory = "2Gi"
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
        env {
          name  = "EVIDENCE_GCS_BUCKET"
          value = google_storage_bucket.evidence.name
        }
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}

resource "google_cloud_run_v2_job" "takedown_agent" {
  project             = var.project_id
  name                = "takedown-agent-job"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.takedown.email
      timeout         = "${var.job_task_timeout_seconds}s"
      max_retries     = var.job_max_retries

      containers {
        image   = var.agents_image
        command = ["python"]
        args    = ["takedown_agent.py"]

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
        # Explicito, nao so o default do codigo (regra #3 do CLAUDE.md --
        # "DRY_RUN=true e o padrao... nunca disparar takedown durante
        # testes ou gravacao de demo"). Trocar isso exige alterar este
        # arquivo (revisavel em PR/plan), nunca uma env var esquecida.
        env {
          name  = "DRY_RUN"
          value = "true"
        }
      }
    }
  }

  depends_on = [google_project_service.required, data.google_artifact_registry_repository.sentinel_images]
}
