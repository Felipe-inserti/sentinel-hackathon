# Sentinel -- imagem compartilhada dos processos SEM Playwright: o
# agent-gateway (Cloud Run Service) e os workers ct-listener/orchestrator/
# takedown-agent (Cloud Run Jobs, sob demanda -- ver infra/README.md).
# evidence_agent.py usa uma imagem SEPARADA (Dockerfile.evidence, base
# Playwright/Chromium) -- os outros tres nunca abrem um browser, entao nao
# ha motivo pra pagar esse peso na imagem deles.
#
# UMA imagem, comando/args diferentes por servico -- Cloud Run Service e
# Cloud Run Job aceitam `command`/`args` por recurso (ver
# infra/cloud_run_gateway.tf/cloud_run_jobs.tf), entao nao ha necessidade
# de 4 Dockerfiles quase identicos so pra trocar o entrypoint.
#
# Python 3.12 fixado explicitamente (nao 3.14) -- 3.14 causou
# incompatibilidades no ambiente de desenvolvimento local deste sprint.
#
# Build multi-stage: dependencias instaladas num venv isolado no estagio
# `builder` (com gcc/build-essential, que varias wheels C precisam --
# Levenshtein, cryptography), copiado pronto pro estagio final SEM as
# ferramentas de build -- mesma ideia do dashboard/Dockerfile (imagem
# final minima), adaptada pra Python.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runner

RUN groupadd --system sentinel && useradd --system --gid sentinel --create-home sentinel

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Codigo dos 4 processos que esta imagem serve (ver .dockerignore -- nunca
# copia dashboard/, tests/, infra/, venvs locais). `evidence_agent.py`
# entra aqui tambem: `takedown_agent.py` importa duas funcoes dele
# (`_collect_rdap_domain`/`_extract_vcard_field`, reuso de RDAP -- ver
# comentario no proprio takedown_agent.py) -- so a definicao do modulo,
# NUNCA abre um browser Playwright nesses caminhos, entao nao precisa da
# imagem pesada do Chromium (ver Dockerfile.evidence) so por causa deste
# import.
COPY config.py llm_client.py registry.py sanitizer.py telemetry.py \
     agent_gateway.py takedown.py takedown_agent.py brand_agent.py \
     brand_memory.py gemma_triage.py evidence_agent.py ./
COPY plane1_ingestion/ ./plane1_ingestion/
COPY plane2_agents/ ./plane2_agents/

RUN chown -R sentinel:sentinel /app
USER sentinel

EXPOSE 8080

# Default = agent-gateway (o unico dos quatro que de fato serve HTTP).
# ct-listener-job/orchestrator-job/takedown-agent-job sobrescrevem
# `command`/`args` no Cloud Run Job (ver infra/cloud_run_jobs.tf) --
# nenhum precisa de uma imagem propria so por isso.
CMD ["uvicorn", "agent_gateway:app", "--host", "0.0.0.0", "--port", "8080"]
