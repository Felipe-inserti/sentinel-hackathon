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
  description = "Timeout MAXIMO de uma execucao de Cloud Run Job (ct-listener/orchestrator/evidence-collector/takedown-agent) -- todos os 4 rodam um loop infinito (websocket ou pull do Pub/Sub) ate serem cancelados manualmente OU baterem este timeout, o que vier primeiro. Deliberadamente NAO o maximo permitido pela plataforma (24h): um valor menor limita o pior caso de custo se voce esquecer de cancelar a execucao apos a demo (ver teardown.sh) -- Cloud Run Jobs so cobram enquanto uma execucao esta rodando (sem cobranca em repouso, confirmado contra a documentacao oficial nesta sessao), entao o teto real de 'esquecimento' e este timeout, nao o preco por hora. Default 3h -- cobre preparo + gravacao + folga, sem deixar rodando indefinidamente."
  type        = number
  default     = 10800
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

variable "monthly_budget_usd" {
  description = "Teto do orcamento mensal (USD) para o alerta de billing (ver budget.tf). So gera notificacao -- nao interrompe nem limita gastos automaticamente (Cloud Billing Budgets nao faz isso sem uma automacao extra, fora do escopo deste sprint)."
  type        = number
  default     = 25
}
