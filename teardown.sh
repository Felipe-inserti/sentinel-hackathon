#!/usr/bin/env bash
#
# Sentinel - teardown.sh (Sprint 8, Parte B)
#
# Nao apaga infraestrutura (Service Accounts, topicos, Firestore, bucket
# de evidencia continuam existindo -- sao baratos/gratis em repouso e
# redeployar do zero e mais lento/arriscado que so garantir que nada esta
# GASTANDO). O que de fato custa dinheiro neste projeto quando ativo:
#
#   1. Execucoes de Cloud Run Job ainda RODANDO (ct-listener/orchestrator/
#      evidence-collector/takedown-agent) -- cobram por CPU/memoria
#      enquanto a execucao esta ativa. Cloud Run Jobs NAO cobram nada
#      entre execucoes (confirmado contra a documentacao oficial no
#      Sprint 8 Parte B) -- o risco real de "esqueci de rodar isso depois
#      da demo" e uma execucao que ficou rodando, nao o Job "existir".
#   2. Cloud Run Services (agent-gateway, e o do Gemma se deployado) com
#      min-instances > 0 -- ambos sao criados com min-instances=0 por
#      padrao (ver infra/), mas este script CONFIRMA e corrige se alguem
#      mudou manualmente.
#
# Idempotente: rodar sem nada pendente imprime "nada a derrubar" e sai
# com sucesso -- seguro rodar de novo, ou rodar sem saber se ja rodou.
#
# Uso:
#   ./teardown.sh [PROJECT_ID] [REGION]

set -uo pipefail  # sem -e: uma falha isolada (ex: um job que nao existe)
                   # nao pode interromper a checagem dos outros -- este
                   # script tem que tentar TUDO e reportar no final.

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${2:-us-central1}"

JOBS=(ct-listener-job orchestrator-job evidence-collector-job takedown-agent-job)
SERVICES=(sentinel-agent-gateway sentinel-gemma-triage)

log()  { printf '\033[1;34m[teardown]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[teardown]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[teardown]\033[0m %s\n' "$*" >&2; }

if [[ -z "$PROJECT_ID" ]]; then
  err "Nenhum PROJECT_ID informado e nenhum projeto ativo no gcloud config."
  err "Uso: ./teardown.sh <PROJECT_ID> [REGION]"
  exit 1
fi

log "Projeto: ${PROJECT_ID} (regiao: ${REGION})"
CANCELLED_COUNT=0
FIXED_SERVICE_COUNT=0

echo
log "1/2 -- Cancelando execucoes de Job ainda em andamento..."
for job in "${JOBS[@]}"; do
  if ! gcloud run jobs describe "$job" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    warn "  ${job}: nao existe (Terraform nao aplicado, ou nome diferente) -- pulando."
    continue
  fi

  # Deliberadamente SEM filtro por status: nao confiamos num nome de campo
  # de filtro nao verificado por execucao real contra a API (nenhum Job
  # existia ainda quando este script foi escrito, ver relatorio da
  # sessao). Em vez disso, tentamos cancelar as ultimas execucoes
  # recentes INCONDICIONALMENTE -- `executions cancel` numa execucao que
  # ja terminou devolve erro (estado terminal), que so tratamos como
  # "nada a fazer" e seguimos; e um script de seguranca, prefere tentar
  # demais a filtrar errado e deixar algo rodando.
  recent_executions="$(gcloud run jobs executions list \
    --job="$job" --region="$REGION" --project "$PROJECT_ID" \
    --format="value(metadata.name)" --limit=5 2>/dev/null || true)"

  if [[ -z "$recent_executions" ]]; then
    log "  ${job}: nenhuma execucao encontrada (nunca rodou)."
    continue
  fi

  while IFS= read -r execution; do
    [[ -z "$execution" ]] && continue
    if gcloud run jobs executions cancel "$execution" \
        --region="$REGION" --project "$PROJECT_ID" --quiet >/dev/null 2>&1; then
      log "  ${job}: execucao '${execution}' cancelada (estava rodando)."
      CANCELLED_COUNT=$((CANCELLED_COUNT + 1))
    fi
    # Sem 'else'/warn aqui de proposito: a grande maioria das execucoes
    # recentes ja estara concluida (comportamento esperado, nao um erro) --
    # avisar a cada uma seria ruido, nao sinal. O resumo final (abaixo)
    # mostra quantas realmente precisaram ser canceladas.
  done <<< "$recent_executions"
done

echo
log "2/2 -- Confirmando min-instances=0 nos Cloud Run Services..."
for service in "${SERVICES[@]}"; do
  if ! gcloud run services describe "$service" --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1; then
    warn "  ${service}: nao existe -- pulando."
    continue
  fi

  min_instances="$(gcloud run services describe "$service" \
    --project "$PROJECT_ID" --region "$REGION" \
    --format="value(spec.template.metadata.annotations.'autoscaling.knative.dev/minScale')" 2>/dev/null || echo "0")"
  min_instances="${min_instances:-0}"

  if [[ "$min_instances" != "0" ]]; then
    warn "  ${service}: min-instances=${min_instances} (deveria ser 0) -- corrigindo..."
    if gcloud run services update "$service" \
        --project "$PROJECT_ID" --region "$REGION" --min-instances=0 --quiet >/dev/null 2>&1; then
      log "    -> corrigido para 0."
      FIXED_SERVICE_COUNT=$((FIXED_SERVICE_COUNT + 1))
    else
      err "    -> falha ao corrigir '${service}' -- verifique manualmente:"
      err "       gcloud run services update ${service} --project ${PROJECT_ID} --region ${REGION} --min-instances=0"
    fi
  else
    log "  ${service}: ja em min-instances=0."
  fi
done

echo
log "Resumo: ${CANCELLED_COUNT} execucao(oes) de Job cancelada(s), ${FIXED_SERVICE_COUNT} servico(s) corrigido(s) para min-instances=0."
if [[ "$CANCELLED_COUNT" -eq 0 && "$FIXED_SERVICE_COUNT" -eq 0 ]]; then
  log "Nada estava rodando -- custo ja proximo de zero."
fi
log "Infraestrutura (Service Accounts, topicos Pub/Sub, Firestore, bucket de"
log "evidencia, o proprio agent-gateway/Jobs como RECURSOS) continua existindo --"
log "isso nao custa nada parado. Para redeployar depois, rode ./deploy.sh de novo."
