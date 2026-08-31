#!/usr/bin/env bash
#
# Envio real do e-mail de demo (Cena 3) -- DRY_RUN=false SÓ neste processo,
# nunca persistido. A senha de app SMTP e lida aqui, na sua tela, com
# `read -s` (nao ecoa, nao vai pra historico do shell, nao passa por
# nenhum chat) -- nunca colada em lugar nenhum.
#
# Pre-requisito: o dominio abaixo ja precisa estar com
# status=TAKEDOWN_APPROVED no Firestore (aprovado no dashboard,
# categoria brand_protection_vendor) -- senao o script rejeita e audita,
# sem enviar nada.
#
# Uso: ./scripts/send_demo_takedown.sh [destinatario@gmail.com]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOMAIN="sentinel-demo-target-cugvqtrd7q-uc.a.run.app"
RECIPIENT="${1:-felipe.inserti@gmail.com}"
SMTP_USER="felipe.inserti@gmail.com"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

read -srp "Senha de app SMTP de ${SMTP_USER} (16 chars, sem espaço): " SMTP_PASSWORD
echo
echo

DRY_RUN=false \
DEMO_LIVE_SEND_ALLOWLIST="{\"${DOMAIN}\": \"${RECIPIENT}\"}" \
DEMO_SMTP_HOST=smtp.gmail.com \
DEMO_SMTP_PORT=587 \
DEMO_SMTP_USERNAME="${SMTP_USER}" \
DEMO_SMTP_PASSWORD="${SMTP_PASSWORD}" \
python3 -c "
import asyncio
from datetime import datetime, timezone
import registry, takedown_agent as ta

async def main():
    manifest = registry.AgentManifest(
        agent_id='takedown-agent', version='1.0.0', owner_team='sentinel-response',
        description='demo', input_schema={'type': 'object'}, output_schema={'type': 'object'},
        tools_allowed=[], required_permissions=[], sla_seconds=5.0,
        status=registry.AgentStatus.ACTIVE, created_at=datetime.now(timezone.utc),
    )
    out = await ta.process_takedown_approval('${DOMAIN}', manifest)
    print(f'sent={out.sent} dry_run={out.dry_run}')

asyncio.run(main())
"

unset SMTP_PASSWORD
