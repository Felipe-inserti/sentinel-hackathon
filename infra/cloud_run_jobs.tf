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
 * teardown.sh). O `timeout` de cada Job abaixo agora vem de
 * `var.worker_timeouts` (Etapa B, observation_scheduler.tf) -- um valor por
 * worker, nao mais um teto unico de 3h para os 4. Ver a variavel para o
 * desenho de janelas e a fonte do teto real de plataforma (168h/7 dias).
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
      timeout         = "${var.worker_timeouts["ct_listener"]}s"
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

        # Etapa D -- env vars da instrumentacao de observacao (Etapa C).
        # Identicas nos 4 Jobs (ver docstring de var.observation_run_id em
        # variables.tf) -- OBSERVATION_RUN_ID e o UNICO dynamic (so injetado
        # quando nao vazio, mesmo padrao de GEMMA_OLLAMA_BASE_URL logo
        # abaixo): vazio == env var nem existe == Settings.observation_run_id
        # cai no None default == observation_run.py inteiro vira no-op.
        env {
          name  = "OBSERVATION_RUNS_COLLECTION"
          value = var.observation_runs_collection
        }
        env {
          name  = "OBSERVATION_COST_GUARD_USD_LIMIT"
          value = tostring(var.observation_cost_guard_usd_limit)
        }
        env {
          name  = "OBSERVATION_CHECKPOINT_INTERVAL_SECONDS"
          value = tostring(var.observation_checkpoint_interval_seconds)
        }
        env {
          name  = "OBSERVATION_PREFILTER_ESCAPE_RATE_THRESHOLD"
          value = tostring(var.observation_prefilter_escape_rate_threshold)
        }
        env {
          name  = "OBSERVATION_ANOMALY_MIN_SAMPLE_SIZE"
          value = tostring(var.observation_anomaly_min_sample_size)
        }
        dynamic "env" {
          for_each = var.observation_run_id != "" ? [var.observation_run_id] : []
          content {
            name  = "OBSERVATION_RUN_ID"
            value = env.value
          }
        }
        # DRY_RUN=true explicito nos 4 Jobs (Etapa D), nao so no
        # takedown-agent -- mesmo raciocinio do bloco original abaixo
        # (regra #3 do CLAUDE.md): trocar isso exige alterar este arquivo
        # (revisavel em PR/plan), nunca uma env var esquecida. ct-listener/
        # orchestrator/evidence-collector nao leem DRY_RUN hoje (so
        # takedown_agent.py/observation_run.enforce_dry_run_lock leem), mas
        # ganham a env var mesmo assim -- consistencia entre os 4 workers,
        # e pronta para o dia em que outro worker precisar dela.
        env {
          name  = "DRY_RUN"
          value = "true"
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
      timeout         = "${var.worker_timeouts["orchestrator"]}s"
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

        # Etapa D -- env vars da instrumentacao de observacao (Etapa C).
        # Identicas nos 4 Jobs -- ver comentario completo no bloco de
        # ct_listener acima (mesma docstring de var.observation_run_id em
        # variables.tf). orchestrator.py e o UNICO worker que le
        # OBSERVATION_COST_GUARD_USD_LIMIT de verdade hoje
        # (observation_run.cost_guard_allows_llm_call).
        env {
          name  = "OBSERVATION_RUNS_COLLECTION"
          value = var.observation_runs_collection
        }
        env {
          name  = "OBSERVATION_COST_GUARD_USD_LIMIT"
          value = tostring(var.observation_cost_guard_usd_limit)
        }
        env {
          name  = "OBSERVATION_CHECKPOINT_INTERVAL_SECONDS"
          value = tostring(var.observation_checkpoint_interval_seconds)
        }
        env {
          name  = "OBSERVATION_PREFILTER_ESCAPE_RATE_THRESHOLD"
          value = tostring(var.observation_prefilter_escape_rate_threshold)
        }
        env {
          name  = "OBSERVATION_ANOMALY_MIN_SAMPLE_SIZE"
          value = tostring(var.observation_anomaly_min_sample_size)
        }
        dynamic "env" {
          for_each = var.observation_run_id != "" ? [var.observation_run_id] : []
          content {
            name  = "OBSERVATION_RUN_ID"
            value = env.value
          }
        }
        env {
          name  = "DRY_RUN"
          value = "true"
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
      timeout         = "${var.worker_timeouts["evidence_collector"]}s"
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

        # Etapa D -- env vars da instrumentacao de observacao (Etapa C).
        # Identicas nos 4 Jobs -- ver comentario completo no bloco de
        # ct_listener acima. evidence_agent.py NAO chama observation_run.py
        # ainda (Etapa C nao instrumentou este worker -- so
        # ct_listener.py/orchestrator.py/takedown_agent.py) -- injetada
        # mesmo assim por consistencia entre os 4 workers e para nao exigir
        # outra mudanca de infra no dia em que evidence-collector ganhar
        # essa instrumentacao.
        env {
          name  = "OBSERVATION_RUNS_COLLECTION"
          value = var.observation_runs_collection
        }
        env {
          name  = "OBSERVATION_COST_GUARD_USD_LIMIT"
          value = tostring(var.observation_cost_guard_usd_limit)
        }
        env {
          name  = "OBSERVATION_CHECKPOINT_INTERVAL_SECONDS"
          value = tostring(var.observation_checkpoint_interval_seconds)
        }
        env {
          name  = "OBSERVATION_PREFILTER_ESCAPE_RATE_THRESHOLD"
          value = tostring(var.observation_prefilter_escape_rate_threshold)
        }
        env {
          name  = "OBSERVATION_ANOMALY_MIN_SAMPLE_SIZE"
          value = tostring(var.observation_anomaly_min_sample_size)
        }
        dynamic "env" {
          for_each = var.observation_run_id != "" ? [var.observation_run_id] : []
          content {
            name  = "OBSERVATION_RUN_ID"
            value = env.value
          }
        }
        env {
          name  = "DRY_RUN"
          value = "true"
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
      timeout         = "${var.worker_timeouts["takedown_agent"]}s"
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

        # Etapa D -- env vars da instrumentacao de observacao (Etapa C).
        # Identicas nos 4 Jobs -- ver comentario completo no bloco de
        # ct_listener acima. takedown_agent.py e o UNICO worker que le
        # OBSERVATION_RUN_ID para uma decisao de seguranca de verdade
        # (observation_run.enforce_dry_run_lock, chamado no __main__ --
        # recusa iniciar se um run estiver ativo com DRY_RUN=false).
        env {
          name  = "OBSERVATION_RUNS_COLLECTION"
          value = var.observation_runs_collection
        }
        env {
          name  = "OBSERVATION_COST_GUARD_USD_LIMIT"
          value = tostring(var.observation_cost_guard_usd_limit)
        }
        env {
          name  = "OBSERVATION_CHECKPOINT_INTERVAL_SECONDS"
          value = tostring(var.observation_checkpoint_interval_seconds)
        }
        env {
          name  = "OBSERVATION_PREFILTER_ESCAPE_RATE_THRESHOLD"
          value = tostring(var.observation_prefilter_escape_rate_threshold)
        }
        env {
          name  = "OBSERVATION_ANOMALY_MIN_SAMPLE_SIZE"
          value = tostring(var.observation_anomaly_min_sample_size)
        }
        dynamic "env" {
          for_each = var.observation_run_id != "" ? [var.observation_run_id] : []
          content {
            name  = "OBSERVATION_RUN_ID"
            value = env.value
          }
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
