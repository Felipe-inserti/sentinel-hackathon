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
