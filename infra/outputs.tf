output "ct_listener_sa_email" {
  description = "E-mail da Service Account de ct-listener."
  value       = google_service_account.ct_listener.email
}

output "orchestrator_sa_email" {
  description = "E-mail da Service Account de orchestrator."
  value       = google_service_account.orchestrator.email
}

output "evidence_sa_email" {
  description = "E-mail da Service Account de evidence-collector."
  value       = google_service_account.evidence.email
}

output "takedown_sa_email" {
  description = "E-mail da Service Account de takedown-agent (a mais restrita)."
  value       = google_service_account.takedown.email
}

output "dashboard_sa_email" {
  description = "E-mail da Service Account do dashboard."
  value       = google_service_account.dashboard.email
}

output "evidence_bucket_name" {
  description = "Nome do bucket GCS de evidencia."
  value       = google_storage_bucket.evidence.name
}

output "evidence_subscription_id" {
  description = "Subscription exclusiva de evidence-sa sobre investigation-completed (criada por este Terraform)."
  value       = google_pubsub_subscription.sub_evidence.name
}

output "takedown_topic_id" {
  description = "Topico Pub/Sub de aprovacao de takedown (criado por este Terraform)."
  value       = google_pubsub_topic.takedown_approved.name
}

output "takedown_subscription_id" {
  description = "Subscription exclusiva de takedown-sa (criada por este Terraform)."
  value       = google_pubsub_subscription.sub_takedown.name
}

# --- Sprint 8, Parte B (Deploy) ---------------------------------------------

output "gateway_sa_email" {
  description = "E-mail da Service Account do agent-gateway."
  value       = google_service_account.gateway.email
}

output "gateway_url" {
  description = "URL publica do agent-gateway (Cloud Run Service) -- mesmo valor usado para AGENT_GATEWAY_AUDIENCE (ver cloud_run_gateway.tf)."
  value       = google_cloud_run_v2_service.gateway.uri
}

output "artifact_registry_repository_url" {
  description = "URL base do repositorio Docker (para deploy.sh montar as tags de imagem)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${data.google_artifact_registry_repository.sentinel_images.repository_id}"
}

output "cloud_run_job_names" {
  description = "Nomes dos 4 Cloud Run Jobs -- usados por deploy.sh/teardown.sh para 'gcloud run jobs execute/executions cancel'."
  value = {
    ct_listener        = google_cloud_run_v2_job.ct_listener.name
    orchestrator       = google_cloud_run_v2_job.orchestrator.name
    evidence_collector = google_cloud_run_v2_job.evidence_collector.name
    takedown_agent     = google_cloud_run_v2_job.takedown_agent.name
  }
}

# --- Etapa B (Scheduler) -----------------------------------------------------

output "scheduler_sa_email" {
  description = "E-mail da Service Account do Cloud Scheduler (so roles/run.invoker por Job, ver observation_scheduler.tf)."
  value       = google_service_account.scheduler.email
}

output "cloud_scheduler_job_names" {
  description = "Nomes dos 4 Cloud Scheduler jobs criados em observation_scheduler.tf."
  value = {
    ct_listener        = google_cloud_scheduler_job.ct_listener.name
    orchestrator       = google_cloud_scheduler_job.orchestrator.name
    evidence_collector = google_cloud_scheduler_job.evidence_collector.name
    takedown_agent     = google_cloud_scheduler_job.takedown_agent.name
  }
}
