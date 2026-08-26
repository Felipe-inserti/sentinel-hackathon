/**
 * Sprint 8, Parte B -- Artifact Registry.
 *
 * Repositorio Docker para as imagens que `deploy.sh` builda (ver
 * Dockerfile/Dockerfile.evidence na raiz do repo): a build compartilhada
 * dos 3 workers leves + agent-gateway, e a build separada de
 * evidence_agent (Playwright). Distinto do repositorio
 * "cloud-run-source-deploy" que `gcloud run deploy --source .` ja criou
 * sozinho para o dashboard (ver dashboard/README.md).
 *
 * DATA SOURCE, nao `resource` -- CORRIGIDO neste sprint depois de um
 * incidente real: a primeira versao declarava isto como `resource`
 * AQUI e `deploy.sh` tambem criava o mesmo repositorio via `gcloud
 * artifacts repositories create` (etapa 2, ANTES do build de imagens,
 * que precisa do repo ja existir pra fazer push) -- dois donos do
 * mesmo recurso, exatamente o que o comentario original deste arquivo
 * dizia estar evitando. Resultado real: `terraform apply` batia 409
 * ALREADY_EXISTS na segunda vez (o gcloud da etapa 2 ja tinha criado).
 * A correcao segue a MESMA disciplina ja usada pros topicos Pub/Sub em
 * main.tf ("referenciado pelo nome, gerenciado por fora"): `deploy.sh`
 * (gcloud, idempotente, ja tinha o create-se-nao-existir certo) e o
 * UNICO dono; este bloco so LE o repositorio pra outros recursos deste
 * Terraform poderem referenciar `.id`/`.name`, sem tentar criar nada.
 * Terraform SO consegue planejar/aplicar depois que `deploy.sh` (etapa
 * 2) ja rodou pelo menos uma vez -- ordem documentada em README.md e
 * garantida por `deploy.sh` em si (etapa 2 vem antes da etapa 4).
 */

data "google_artifact_registry_repository" "sentinel_images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repository_id
}
