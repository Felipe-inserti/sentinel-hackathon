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
