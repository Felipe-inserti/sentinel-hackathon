/**
 * Etapa B -- execucao continua do run de observacao contra o Certificate
 * Transparency real. Uma `google_cloud_scheduler_job` por worker, chamando
 * a Run Admin API (`:run`) dos 4 `google_cloud_run_v2_job` de
 * cloud_run_jobs.tf -- nao cria Jobs novos, so os aciona na cadencia da
 * tabela abaixo. O timeout de cada Job (atributo mutavel, sem recriar o
 * recurso) veio de `var.worker_timeouts` (variables.tf), editado junto
 * nesta etapa.
 *
 * Teto de plataforma verificado nesta sessao (nao inferido): o
 * `task_timeout` maximo de um Cloud Run Job SEM GPU e 168 HORAS (7 DIAS),
 * nao as 24h que o comentario antigo de `job_task_timeout_seconds`
 * registrava. Fonte:
 * https://docs.cloud.google.com/run/docs/configuring/task-timeout --
 * "By default, each task runs for a maximum of 10 minutes: you can change
 * this to a shorter time or a longer time up to 168 hours (7 days). For
 * tasks using GPUs, the maximum available timeout is 1 hour." Nenhum
 * worker deste projeto usa GPU. Confirmar de novo se este arquivo for
 * revisitado depois de uma mudanca de versao do provider/API -- nao
 * reinferir do zero.
 *
 * Desenho de janelas (restricao de orcamento: teto US$25, var.monthly_
 * budget_usd em budget.tf) -- ver tambem o comentario de
 * `var.worker_timeouts`:
 *
 *   worker              | frequencia                      | timeout
 *   --------------------|----------------------------------|--------
 *   ct-listener         | continuo (re-disparo encadeado)  | 168h (maximo de plataforma)
 *   orchestrator        | 4x/dia, janelas de ~2h           | 2h
 *   evidence-collector  | 2x/dia, janelas de ~1h           | 1h
 *   takedown-agent      | 1x/dia, janela curta             | 15min
 *
 * Racional: certstream (ct-listener) e efemero, sem replay -- qualquer
 * lacuna e evento perdido. Os outros 3 consomem de Pub/Sub, que retem
 * mensagem por 24h (message_retention_duration, main.tf) -- podem ficar
 * desligados fora da janela sem perder nada.
 *
 * ct-listener em detalhe -- "re-disparo encadeado": o cron abaixo dispara
 * exatamente a cada 168h (semanal, `0 0 * * 0`, fuso UTC fixo para nao
 * derivar com horario de verao em outro fuso). Como o timeout do Job E
 * o mesmo periodo, a proxima execucao so precisa comecar quando a
 * plataforma ja forcou o fim da anterior por timeout -- a lacuna entre
 * "a execucao anterior foi morta" e "a proxima comeca" e so o tempo de
 * cold start + reconexao do websocket do certstream, nao um gap de
 * agendamento evitavel. NAO ha protecao aqui contra duas execucoes rodando
 * em paralelo se uma execucao terminar sozinha (crash) antes do timeout e
 * o cron dela ainda nao tiver disparado de novo -- ficaria sem cobertura
 * ate o proximo disparo semanal. Mitigar isso (watchdog que reinicia antes
 * do timeout cheio, ou verifica execucao ativa antes de chamar `:run`)
 * fica como pendencia explicita, fora do escopo desta etapa (so scheduling
 * direto foi pedido).
 *
 * Autenticacao Scheduler -> Run Admin API: OAUTH token (nao OIDC) -- a
 * Run Admin API (`run.googleapis.com`) e uma API do Google Cloud, nao um
 * Cloud Run Service com IAM de invocacao por audience; OIDC (usado em
 * cloud_run_gateway.tf para a autenticacao PROPRIA do agent_gateway) nao
 * se aplica aqui. Confirmado contra o schema do provider instalado nesta
 * sessao (`terraform providers schema -json`, google_cloud_scheduler_job
 * ->  http_target -> oauth_token{scope, service_account_email}) e contra
 * https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule.
 *
 * IAM de scheduler-sa: `roles/run.invoker` por BINDING DE JOB
 * (`google_cloud_run_v2_job_iam_member`, escopo apertado -- so os 4 Jobs
 * que ela precisa acionar), nao `roles/run.developer`/papel de projeto.
 * Confirmado que o recurso de binding por job existe no provider instalado
 * (7.45.0) via `terraform providers schema -json` nesta sessao.
 */

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "scheduler-sa"
  display_name = "Sentinel - cloud-scheduler"
  description  = "Dispara execucoes dos 4 Cloud Run Jobs (Run Admin API :run) na cadencia de observation_scheduler.tf. Unica permissao: roles/run.invoker, um binding por Job (escopo apertado, nao papel de projeto)."
}

# ---------------------------------------------------------------------------
# IAM -- roles/run.invoker por Job (nao por projeto).
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke_ct_listener" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.ct_listener.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke_orchestrator" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.orchestrator.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke_evidence_collector" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.evidence_collector.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke_takedown_agent" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.takedown_agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler -- um job por worker, POST em .../jobs/{name}:run.
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "ct_listener" {
  project     = var.project_id
  region      = var.region
  name        = "sentinel-run-ct-listener"
  description = "Re-disparo semanal (encadeado) de ct-listener-job -- periodo igual ao timeout do Job (var.worker_timeouts.ct_listener, 168h/7 dias, teto de plataforma) para minimizar a lacuna entre execucoes. certstream nao tem replay -- ver docstring do arquivo."
  schedule    = "0 0 * * 0" # todo domingo 00:00 UTC -- exatamente 168h de intervalo
  time_zone   = "Etc/UTC"

  # Causa raiz do incidente descoberto nesta sessao: o apply da Etapa C+D
  # criou os 4 jobs deste arquivo ja ENABLED (comportamento padrao do
  # recurso quando `paused` nao e setado), e ficaram disparando em cron
  # sob um `observation_run_id` que ninguem pretendia rodar -- descoberto
  # so quando o orchestrator ja tinha custado ~US$7 de Gemini. `paused`
  # e atributo real do provider (confirmado via `terraform providers
  # schema -json`, google v7.45.0 -- nao e so o `state` computed).
  # Todo apply a partir de agora sobe os 4 jobs PAUSADOS por padrao --
  # habilitar exige um passo humano explicito (`gcloud scheduler jobs
  # resume <nome> --location=us-central1`), nunca um efeito colateral de
  # `terraform apply`.
  paused = true

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.ct_listener.name}:run"
    http_method = "POST"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_job_iam_member.scheduler_invoke_ct_listener,
  ]
}

resource "google_cloud_scheduler_job" "orchestrator" {
  project     = var.project_id
  region      = var.region
  name        = "sentinel-run-orchestrator"
  description = "4x/dia (a cada 6h), janela de ~2h (var.worker_timeouts.orchestrator) -- Pub/Sub retem mensagem por 24h, pode ficar desligado fora da janela."
  schedule    = "0 0,6,12,18 * * *" # 00h, 06h, 12h, 18h UTC
  time_zone   = "Etc/UTC"

  # Causa raiz do incidente descoberto nesta sessao: o apply da Etapa C+D
  # criou os 4 jobs deste arquivo ja ENABLED (comportamento padrao do
  # recurso quando `paused` nao e setado), e ficaram disparando em cron
  # sob um `observation_run_id` que ninguem pretendia rodar -- descoberto
  # so quando o orchestrator ja tinha custado ~US$7 de Gemini. `paused`
  # e atributo real do provider (confirmado via `terraform providers
  # schema -json`, google v7.45.0 -- nao e so o `state` computed).
  # Todo apply a partir de agora sobe os 4 jobs PAUSADOS por padrao --
  # habilitar exige um passo humano explicito (`gcloud scheduler jobs
  # resume <nome> --location=us-central1`), nunca um efeito colateral de
  # `terraform apply`.
  paused = true

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.orchestrator.name}:run"
    http_method = "POST"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_job_iam_member.scheduler_invoke_orchestrator,
  ]
}

resource "google_cloud_scheduler_job" "evidence_collector" {
  project     = var.project_id
  region      = var.region
  name        = "sentinel-run-evidence-collector"
  description = "2x/dia (a cada 12h), janela de ~1h (var.worker_timeouts.evidence_collector) -- Pub/Sub retem mensagem por 24h, pode ficar desligado fora da janela."
  schedule    = "0 0,12 * * *" # 00h, 12h UTC
  time_zone   = "Etc/UTC"

  # Causa raiz do incidente descoberto nesta sessao: o apply da Etapa C+D
  # criou os 4 jobs deste arquivo ja ENABLED (comportamento padrao do
  # recurso quando `paused` nao e setado), e ficaram disparando em cron
  # sob um `observation_run_id` que ninguem pretendia rodar -- descoberto
  # so quando o orchestrator ja tinha custado ~US$7 de Gemini. `paused`
  # e atributo real do provider (confirmado via `terraform providers
  # schema -json`, google v7.45.0 -- nao e so o `state` computed).
  # Todo apply a partir de agora sobe os 4 jobs PAUSADOS por padrao --
  # habilitar exige um passo humano explicito (`gcloud scheduler jobs
  # resume <nome> --location=us-central1`), nunca um efeito colateral de
  # `terraform apply`.
  paused = true

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.evidence_collector.name}:run"
    http_method = "POST"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_job_iam_member.scheduler_invoke_evidence_collector,
  ]
}

resource "google_cloud_scheduler_job" "takedown_agent" {
  project     = var.project_id
  region      = var.region
  name        = "sentinel-run-takedown-agent"
  description = "1x/dia, janela curta (var.worker_timeouts.takedown_agent, 15min -- escolha de projeto, nao teto de plataforma) -- reconfirma aprovacoes pendentes; Pub/Sub retem mensagem por 24h."
  schedule    = "0 0 * * *" # 00h UTC, diario
  time_zone   = "Etc/UTC"

  # Causa raiz do incidente descoberto nesta sessao: o apply da Etapa C+D
  # criou os 4 jobs deste arquivo ja ENABLED (comportamento padrao do
  # recurso quando `paused` nao e setado), e ficaram disparando em cron
  # sob um `observation_run_id` que ninguem pretendia rodar -- descoberto
  # so quando o orchestrator ja tinha custado ~US$7 de Gemini. `paused`
  # e atributo real do provider (confirmado via `terraform providers
  # schema -json`, google v7.45.0 -- nao e so o `state` computed).
  # Todo apply a partir de agora sobe os 4 jobs PAUSADOS por padrao --
  # habilitar exige um passo humano explicito (`gcloud scheduler jobs
  # resume <nome> --location=us-central1`), nunca um efeito colateral de
  # `terraform apply`.
  paused = true

  http_target {
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.takedown_agent.name}:run"
    http_method = "POST"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_job_iam_member.scheduler_invoke_takedown_agent,
  ]
}
