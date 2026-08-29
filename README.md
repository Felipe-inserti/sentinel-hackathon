# README.md — estrutura exigida pela submissão

> Este arquivo é o **esqueleto**. Os comandos entre `[ ]` você já tem no
> `docs/DEMO_COMMANDS.md` e no `deploy.sh` — mova-os para cá.
>
> **Exigência do regulamento:** as instruções precisam ser reproduzíveis.
> Antes de submeter, clone o repositório numa pasta limpa, siga suas próprias
> instruções do zero e cronometre. Se você travar, o jurado trava.

---

# Sentinel

`[ badge do GitHub Actions ]`

Detecção e mitigação de campanhas de phishing em tempo real contra marcas
brasileiras, a partir do Certificate Transparency.

**Trilha:** Fortified Enterprise Fleet · **Demo:** `[ link do vídeo ]`

---

## O problema

`[ 2 parágrafos — mesmo conteúdo do Devpost ]`

## A tese: cascata de custo

`[ diagrama + as 6 camadas ]`

---

## Spin-up: rodar localmente

### Pré-requisitos

- Python 3.12
- Conta GCP com billing ativo
- `gcloud` CLI autenticado
- Ollama com Gemma (opcional — o sistema faz *fail-open* sem ele)

### Passo a passo

```bash
git clone https://github.com/Felipe-inserti/sentinel-hackathon
cd sentinel-hackathon

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium

cp .env.example .env
# preencha: GCP_PROJECT_ID, GEMINI_MODEL_ID, GCP_LOCATION, OTEL_REGION

.venv/bin/python -m pytest tests/ -q
# esperado: 345 passed, 3 deselected
```

### Demo local ponta a ponta

```bash
# terminal 1 — sobe o alvo de teste
./demo/phishing-target/serve.sh malicious 8000

# terminal 2 — roda o fluxo completo
python demo_run_multimodal_flow.py <domínio> --brand bancoteste --simulate-approval
```

`[ confirme os argumentos exatos contra o arquivo real ]`

---

## Spin-up: deploy na Google Cloud

### 1. APIs e permissões

```bash
[ comandos de gcloud services enable ]
```

### 2. Infraestrutura (Terraform)

```bash
cd infra
terraform init
terraform plan  -var="project_id=<seu-projeto>" [ demais vars ]
terraform apply -var="project_id=<seu-projeto>" [ demais vars ]
```

### 3. Build e push das imagens

```bash
./deploy.sh   # [ confirme se cobre as 4 imagens ]
```

**⚠️ Duas armadilhas documentadas:**

1. As imagens usam tag mutável (`:latest`). O Terraform compara *strings*, não
   digests — um push novo não gera diff. É preciso
   `terraform apply -replace=google_cloud_run_v2_job.<job>` para forçar a
   re-resolução da tag.
2. Todo `-replace` num Cloud Run Job **derruba o IAM binding** sem sinalizar,
   porque o plano é calculado antes da destruição. Sempre rode um segundo
   `terraform apply -target=google_cloud_run_v2_job_iam_member.<binding>` e
   confirme com `gcloud run jobs get-iam-policy <job> --region=us-central1`.

### 4. Verificação

```bash
[ comando de trace — docs/DEMO_COMMANDS.md §11.1 ]
[ comando de métricas — §11.2 ]
```

Prefixo real das métricas: `prometheus.googleapis.com/<nome>/counter`,
resource type `prometheus_target`.

---

## Arquitetura

`[ diagrama em swimlanes, com custo anotado nas setas ]`

---

## Mapa: requisito da trilha → arquivo → linha

| Requisito | Implementação | Arquivo |
|---|---|---|
| Agent Registry | `agent_registry` no Firestore | `registry.py` |
| Agent Runtime | 4 Cloud Run Jobs sob demanda | `infra/cloud_run_jobs.tf` |
| Memory Bank | memória de marca como few-shot | `brand_memory.py` |
| Agent Identity | 6 Service Accounts, privilégio mínimo | `infra/main.tf` |
| Agent Gateway | roteamento com policy fechada | `agent_gateway.py` |
| Model Armor | sanitização adversarial | `sanitizer.py` |
| Agent Observability | OpenTelemetry → Trace + Monitoring | `telemetry.py` |

`[ preencha as linhas exatas ]`

---

## O que acontece quando quebra

| Falha | Comportamento |
|---|---|
| Gemma indisponível | *Fail-open* — segue para investigação normal |
| Site alvo fora do ar | Bundle parcial, pipeline não quebra |
| Screenshot falha | `visual_analysis_available=false`, segue só com texto |
| RDAP envenenado | Contato rejeitado, nada é enviado |
| LLM devolve schema inválido | Retry, depois falha auditável |
| Mensagem Pub/Sub replicada | Dupla verificação no Firestore rejeita |
| Injeção no conteúdo raspado | Detectada, vira sinal de maliciosidade |

---

## Segurança

`[ as 7 regras inegociáveis do projeto ]`

## Limitações conhecidas

`[ Firestore sem IAM por coleção · lacuna do ct_listener · nam5 ]`

## Findings

Ver [`FINDINGS.md`](FINDINGS.md) e [`docs/RED_TEAM.md`](docs/RED_TEAM.md).

## Teardown

```bash
./teardown.sh
```
