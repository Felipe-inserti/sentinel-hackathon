"""Configuracao centralizada do Sentinel.

Todo modulo que precisa de um valor de configuracao (projeto GCP, nomes de
topico/subscription, ID do modelo Gemini, etc.) importa `settings` daqui.
Nenhum outro modulo deve ler `os.environ` diretamente para esse fim -- isso
evita fontes de verdade divergentes e garante que a validacao aconteca uma
unica vez, na inicializacao do processo.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    gcp_project_id: str
    gcp_location: str = "us-central1"

    # Sem default deliberadamente: o ID de modelo Gemini muda com frequencia
    # e nunca deve ser um valor chutado no codigo -- quem sobe o servico
    # declara explicitamente qual versao validou. Ver .env.example.
    gemini_model_id: str

    # Envio real de notificacao/takedown exige opt-in explicito. Nunca
    # disparar acao externa durante testes ou gravacao de demo.
    dry_run: bool = True

    firestore_collection: str = "investigations"

    suspicious_topic_id: str = "suspicious-domain-detected"
    completed_topic_id: str = "investigation-completed"
    orchestrator_subscription_id: str = "sub-orchestrator"

    # Telemetria (ver telemetry.py). Desligar para testes locais/CI -- sem
    # isso, cada teste tentaria abrir um canal gRPC autenticado de verdade
    # contra telemetry.googleapis.com.
    otel_enabled: bool = True

    # Preco por 1M de tokens (USD), usado so para estimar custo/economia --
    # nao vem de nenhuma API de billing. Os defaults abaixo sao o preco
    # promocional oficial do gemini-3.6-flash (o exemplo sugerido em
    # GEMINI_MODEL_ID no .env.example) valido ate 2026-12-31, verificado em
    # ai.google.dev/gemini-api/docs/pricing -- se o modelo configurado for
    # outro, ajuste estas duas variaveis, senao o numero do pitch fica
    # errado silenciosamente.
    gemini_input_price_per_million_usd: float = 0.75
    gemini_output_price_per_million_usd: float = 3.75

    metrics_firestore_collection: str = "metrics"

    # Camada de triagem Gemma (ver gemma_triage.py). Default e o endereco
    # padrao do `ollama serve` local (convencao do proprio Ollama, nao um
    # chute) -- bom para dev/teste. Em producao, aponte para a URL real do
    # Cloud Run apos o deploy (ver scripts/deploy_gemma_cloudrun.sh);
    # esquecer de trocar e obvio (a chamada falha e cai no fail-open), ao
    # contrario de um ID de modelo errado que erraria o custo em silencio.
    gemma_ollama_base_url: str = "http://localhost:11434"
    gemma_model_id: str = "gemma3:270m"
    gemma_batch_window_seconds: float = 2.0
    # Medido empiricamente (ver FINDINGS.md e eval_triage.py) contra um
    # Gemma 3 270M real: lotes de 5 itens completam o schema JSON com
    # confiabilidade (3/3 nos testes); a partir de ~8 itens o modelo
    # degenera (trunca/repete) quase toda vez. 20 (valor de exemplo do
    # requisito original) e otimista demais para um modelo desse tamanho
    # com este prompt -- fica documentado aqui em vez de silenciosamente
    # depender do fail-open o tempo todo.
    gemma_batch_max_size: int = 5
    gemma_request_timeout_seconds: float = 15.0
    gemma_max_retries: int = 1
    triage_discard_collection: str = "triage_discards"

    # Recursos do container Cloud Run (ver scripts/deploy_gemma_cloudrun.sh)
    # -- usados so para estimar custo de CPU/memoria por chamada.
    gemma_cloud_run_vcpu_count: float = 1.0
    gemma_cloud_run_memory_gib: float = 1.0

    # Preco Cloud Run Tier 1 (us-central1), verificado via busca agregada
    # ao preco oficial em cloud.google.com/run/pricing -- a pagina oficial
    # nao pode ser lida diretamente aqui (conteudo truncado na ferramenta
    # de fetch), entao trate como aproximacao, nao como fonte de billing.
    cloud_run_cpu_price_per_vcpu_second_usd: float = 0.000024
    cloud_run_memory_price_per_gib_second_usd: float = 0.0000025


settings = Settings()
