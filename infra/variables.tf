variable "project_id" {
  description = "ID do projeto GCP onde a identidade dos agentes (service accounts + IAM) e provisionada. O mesmo projeto usado por config.py (GCP_PROJECT_ID)."
  type        = string
}

variable "region" {
  description = "Regiao GCP padrao -- mesma usada por config.py/Vertex AI e pelo bucket de evidencia."
  type        = string
  default     = "us-central1"
}

variable "suspicious_topic_id" {
  description = "Topico Pub/Sub onde ct_listener.py publica dominios suspeitos. Ja criado por scripts/setup_gcp.sh -- NAO gerenciado como recurso por este Terraform, so referenciado pelo nome para os IAM bindings (evita dois donos do mesmo recurso)."
  type        = string
  default     = "suspicious-domain-detected"
}

variable "completed_topic_id" {
  description = "Topico Pub/Sub onde orchestrator.py publica investigacoes concluidas. Ja criado por scripts/setup_gcp.sh -- mesma logica de suspicious_topic_id acima."
  type        = string
  default     = "investigation-completed"
}

variable "orchestrator_subscription_id" {
  description = "Subscription que orchestrator.py consome. Ja criada por scripts/setup_gcp.sh -- so referenciada pelo nome aqui."
  type        = string
  default     = "sub-orchestrator"
}

variable "takedown_topic_id" {
  description = "Topico Pub/Sub de aprovacao de takedown. Ao contrario dos dois acima, nenhum script do projeto criava este topico ainda -- ele passa a ser gerenciado por ESTE Terraform (ver main.tf)."
  type        = string
  default     = "takedown-approved"
}

variable "takedown_subscription_id" {
  description = "Subscription exclusiva de takedown-sa sobre takedown_topic_id -- a UNICA fonte de invocacao permitida por IAM para o agente de takedown."
  type        = string
  default     = "sub-takedown"
}

variable "evidence_bucket_name" {
  description = "Nome do bucket GCS onde evidence-sa grava evidencia de phishing (screenshot/HTML ja redigido de PII). Nomes de bucket GCS sao globalmente unicos -- ajuste se o default colidir com um bucket existente de outro projeto. Default: null (calculado em main.tf como '<project_id>-sentinel-evidence')."
  type        = string
  default     = null
}

variable "evidence_subscription_id" {
  description = "Subscription exclusiva de evidence-sa sobre completed_topic_id (investigation-completed) -- mesmo padrao de orchestrator_subscription_id (uma subscription por consumidor do mesmo topico). Ao contrario daquela, nenhum script do projeto criava esta ainda -- passa a ser gerenciada por ESTE Terraform (mesma logica de takedown_subscription_id)."
  type        = string
  default     = "sub-evidence"
}

# ---------------------------------------------------------------------------
# Sprint 8, Parte B -- Deploy (Agent Gateway + Cloud Run Jobs dos workers).
# ---------------------------------------------------------------------------

variable "artifact_registry_repository_id" {
  description = "Repositorio Docker no Artifact Registry para as imagens do gateway e dos workers Python (ct-listener, orchestrator, evidence-collector, takedown-agent). Separado do repositorio 'cloud-run-source-deploy' que o dashboard ja usa (criado automaticamente por 'gcloud run deploy --source .') -- nao reaproveitado de proposito, para nao misturar o dono (Terraform aqui, gcloud la)."
  type        = string
  default     = "sentinel-images"
}

variable "agents_image" {
  description = "Imagem Docker (com tag) da build compartilhada dos 3 workers leves (ct-listener, orchestrator, takedown-agent) e do agent-gateway -- ver Dockerfile na raiz do repo. deploy.sh builda e publica esta tag ANTES de aplicar o Terraform. SEM DEFAULT DE PROPOSITO (incidente real neste sprint: um placeholder tipo 'us-central1-docker.pkg.dev/PROJECT_ID/...' foi aceito silenciosamente por um 'terraform apply' manual que esqueceu de passar esta -var, e os 5 recursos foram criados apontando pra uma imagem que nao existe -- so foi descoberto quando os Jobs/Service ja estavam no ar. Sem default, falta-la agora e erro/prompt IMEDIATO, nao um recurso quebrado silencioso). deploy.sh SEMPRE passa esta -var; rodar terraform apply manualmente sem ela e o unico jeito de esquecer, e agora falha alto."
  type        = string
}

variable "evidence_image" {
  description = "Imagem Docker (com tag) de evidence_agent -- ver Dockerfile.evidence na raiz do repo (base Playwright, so Chromium). Mesmo motivo de agents_image acima -- SEM DEFAULT DE PROPOSITO, mesmo incidente real corrigido neste sprint."
  type        = string
}

variable "gemini_model_id" {
  description = "GEMINI_MODEL_ID injetado em todo Cloud Run Service/Job deste projeto (config.py::Settings.gemini_model_id nao tem default -- QUALQUER processo que importe config.py falha na inicializacao sem esta env var, mesmo um worker que nao chama o Gemini diretamente, ver ct-listener/takedown-agent). Confirmado pelo ambiente real do projeto -- NAO trocar sem validar contra a documentacao oficial (ver CLAUDE.md)."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "gcp_location_for_vertex" {
  description = "GCP_LOCATION injetado em todo Cloud Run Service/Job -- endpoint do Vertex AI (Gemini). 'global' e o valor real confirmado do projeto (ver config.py); NAO e uma regiao valida para o exportador de metricas OTel, por isso `otel_region` (abaixo) e uma variavel SEPARADA."
  type        = string
  default     = "global"
}

variable "otel_region" {
  description = "OTEL_REGION injetado em todo Cloud Run Service/Job -- regiao usada SO pelo exportador OTel/Telemetry API (ver telemetry.py/config.py::Settings.otel_region). Reproduzido e corrigido neste sprint: sem um 'cloud.region' valido no Resource OTel, o ingest de METRICAS da Telemetry API rejeita com 'Unrecognized region or location' (traces exportam OK sem isso -- so metricas exigem). 'global' (o valor de gcp_location_for_vertex) NAO e uma regiao valida aqui."
  type        = string
  default     = "us-central1"
}

variable "gateway_min_instance_count" {
  description = "Minimo de instancias do agent-gateway (Cloud Run Service). 0 = escala a zero fora de uso, custo zero de instancia ociosa -- unico servico HTTP deste projeto alem do dashboard, entao PODE escalar por requisicao normalmente (ao contrario dos 4 workers, que sao pull loops sem servidor HTTP e por isso viraram Cloud Run Jobs, nao Services -- ver infra/README.md)."
  type        = number
  default     = 0
}

variable "gateway_max_instance_count" {
  description = "Maximo de instancias do agent-gateway. Baixo de proposito (trafego de demo, nao producao em escala)."
  type        = number
  default     = 3
}

variable "job_task_timeout_seconds" {
  description = "Timeout de uma execucao manual de Cloud Run Job (`gcloud run jobs execute`, ver deploy.sh/teardown.sh) fora do fluxo de Scheduler. SUPERSEDIDO para os 4 Jobs abaixo pelo timeout POR WORKER em `var.worker_timeouts` (Etapa B, observation_scheduler.tf) -- cloud_run_jobs.tf nao referencia mais esta variavel. Mantida (nao removida: sprint aditivo) como default conceitual/fallback para quem disparar um Job manualmente sem passar por -var. O teto real confirmado da plataforma NAO e 24h como o comentario anterior desta variavel registrava -- e 168h (7 dias), exceto para tasks com GPU (1h); nenhum worker deste projeto usa GPU. Fonte: https://docs.cloud.google.com/run/docs/configuring/task-timeout (consultada na Etapa B). Default aqui continua 3h -- cobre preparo + gravacao + folga sem rodar indefinidamente por engano."
  type        = number
  default     = 10800
}

# ---------------------------------------------------------------------------
# Etapa B -- Cloud Scheduler + timeout por worker (observation_scheduler.tf).
# ---------------------------------------------------------------------------

variable "worker_timeouts" {
  description = <<-EOT
    Timeout (segundos) do template.template.timeout de CADA um dos 4
    google_cloud_run_v2_job existentes (cloud_run_jobs.tf) -- substitui o
    valor unico de `job_task_timeout_seconds` para esses 4 recursos
    especificamente (mesmos Jobs, apenas o atributo de timeout muda; nenhum
    Job e recriado). Chaves = mesmas usadas em `output.cloud_run_job_names`
    (ct_listener/orchestrator/evidence_collector/takedown_agent).

    Desenho de janelas, restrito pelo teto de orcamento de var.monthly_budget_usd
    (US$25) -- ver infra/README.md, secao "Etapa B -- janelas de observacao":
    so ct-listener perde eventos quando esta off (certstream e efemero, sem
    replay); os outros 3 podem ficar desligados fora da janela porque o
    Pub/Sub retem mensagem por 24h (message_retention_duration em main.tf).

    - ct_listener (604800s = 168h = 7 dias): o TETO MAXIMO confirmado da
      plataforma para Cloud Run Jobs sem GPU (nenhum worker daqui usa GPU).
      Fonte verificada nesta etapa:
      https://docs.cloud.google.com/run/docs/configuring/task-timeout
      ("...up to 168 hours (7 days). For tasks using GPUs, the maximum
      available timeout is 1 hour."). O cron de observation_scheduler.tf
      para ct-listener e alinhado a este MESMO periodo (semanal, UTC) --
      minimiza a lacuna entre execucoes encadeadas: a proxima execucao so
      precisa iniciar quando a anterior bate o teto de tempo da plataforma,
      nunca antes por falta de orcamento de timeout.
    - orchestrator (7200s = 2h): 4x/dia, janelas de ~2h.
    - evidence_collector (3600s = 1h): 2x/dia, janelas de ~1h.
    - takedown_agent (900s = 15min): 1x/dia, janela curta. Este valor,
      diferente do de ct_listener, NAO vem de um teto de plataforma
      verificado -- e uma escolha de projeto (o worker so precisa de uma
      janela curta para reconfirmar aprovacoes pendentes no Firestore);
      documentado aqui como decisao, nao como fato verificado.
  EOT
  type        = map(number)
  default = {
    ct_listener        = 604800
    orchestrator       = 7200
    evidence_collector = 3600
    takedown_agent     = 900
  }
}

variable "job_max_retries" {
  description = "Retries automaticos por execucao de Job, ver template.template.max_retries do google_cloud_run_v2_job. 1 (nao o default de 3 da plataforma) -- da resiliencia a uma queda transitoria (ex: websocket do certstream cair) sem multiplicar o pior caso de custo (retries x job_task_timeout_seconds) por um numero alto se o worker estiver quebrado de verdade."
  type        = number
  default     = 1
}

variable "billing_account_id" {
  description = "ID da conta de faturamento vinculada ao projeto (formato XXXXXX-XXXXXX-XXXXXX), usado pelo orcamento/alerta (ver budget.tf). Confirmado via 'gcloud billing projects describe <project_id>' neste sprint -- NAO um valor generico."
  type        = string
  default     = "01DDB4-8DADFB-76DF50"
}

variable "gemma_ollama_base_url" {
  description = "URL do Cloud Run Service do Gemma/Ollama (ver Dockerfile.gemma/scripts/deploy_gemma_cloudrun.sh), injetada como GEMMA_OLLAMA_BASE_URL no Job de ct-listener (unico consumidor de gemma_triage.py). Vazio por default -- dependencia circular real: a URL so existe DEPOIS do primeiro deploy do servico Gemma, que por sua vez precisa do repositorio de Artifact Registry que este Terraform cria. Fluxo: 1) terraform apply (sem esta variavel), 2) scripts/deploy_gemma_cloudrun.sh, 3) terraform apply de novo com -var=\"gemma_ollama_base_url=<url>\". Vazio faz gemma_triage.py cair no fail-open direto (ver config.py), nao um erro -- a ingestao continua funcionando sem a camada de triagem em lote."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Etapa D -- env vars da instrumentacao de observacao (Etapa C) nos 4 Cloud
# Run Jobs. Ver cloud_run_jobs.tf para onde cada uma e injetada.
# ---------------------------------------------------------------------------

variable "observation_run_id" {
  description = "OBSERVATION_RUN_ID -- injetado nos 4 Jobs (ct-listener/orchestrator/evidence-collector/takedown-agent) so quando NAO vazio (ver dynamic \"env\" em cloud_run_jobs.tf). Precisa ser o MESMO valor nos 4 -- os contadores de observation_run.py sao acumulados de UM run compartilhado entre workers (firestore.Increment em observation_runs/{run_id}), nao por processo. Default vazio de proposito: e uma variavel de ENTRADA (passada explicitamente via -var na janela real de 48h, nunca gerada a cada apply -- um valor novo a cada apply quebraria a resistencia a re-disparo do Scheduler, ver observation_run.py/observation_scheduler.tf). Vazio == env var OBSERVATION_RUN_ID nem chega a existir no container == config.py::Settings.observation_run_id cai no default None == TODA a instrumentacao da Etapa C fica no-op (comportamento identico ao de antes desta Etapa D)."
  type        = string
  default     = ""
}

variable "observation_runs_collection" {
  description = "OBSERVATION_RUNS_COLLECTION -- mesmo default de config.py::Settings.observation_runs_collection, injetado explicitamente (nao depende do default do container bater com o do Terraform)."
  type        = string
  default     = "observation_runs"
}

variable "observation_cost_guard_usd_limit" {
  description = "OBSERVATION_COST_GUARD_USD_LIMIT -- teto de gasto acumulado com LLM (Gemini+Gemma) do run inteiro (ver observation_run.cost_guard_allows_llm_call). Mesmo default de config.py (~US$10, dentro do teto de US$25 do run -- var.monthly_budget_usd)."
  type        = number
  default     = 10
}

variable "observation_checkpoint_interval_seconds" {
  description = "OBSERVATION_CHECKPOINT_INTERVAL_SECONDS -- intervalo do checkpoint periodico (observation_run.checkpoint_loop). Mesmo default de config.py (15 minutos)."
  type        = number
  default     = 900
}

variable "observation_prefilter_escape_rate_threshold" {
  description = "OBSERVATION_PREFILTER_ESCAPE_RATE_THRESHOLD -- limiar do alerta de anomalia (observation_run.check_prefilter_escape_anomaly). Mesmo default de config.py -- escolha de projeto, nao teto de plataforma verificado."
  type        = number
  default     = 0.05
}

variable "observation_anomaly_min_sample_size" {
  description = "OBSERVATION_ANOMALY_MIN_SAMPLE_SIZE -- amostra minima antes do alerta de anomalia avaliar a taxa de escape. Mesmo default de config.py."
  type        = number
  default     = 200
}

variable "monthly_budget_usd" {
  description = "Teto do orcamento mensal (USD) para o alerta de billing (ver budget.tf). So gera notificacao -- nao interrompe nem limita gastos automaticamente (Cloud Billing Budgets nao faz isso sem uma automacao extra, fora do escopo deste sprint)."
  type        = number
  default     = 25
}
