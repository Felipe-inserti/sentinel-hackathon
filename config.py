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
    gcp_location: str = "global"

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

    # evidence_agent.py (Sprint 4). Subscription propria sobre o topico
    # investigation-completed ja existente -- mesmo padrao de
    # sub-orchestrator/sub-takedown (uma subscription por consumidor, ver
    # infra/). evidence_gcs_bucket sem default fixo por design: nomes de
    # bucket GCS sao globalmente unicos, entao o default e calculado em
    # tempo de execucao como "{gcp_project_id}-sentinel-evidence" (mesma
    # formula de infra/main.tf::local.evidence_bucket_name) -- so defina
    # esta variavel se o nome calculado colidir com um bucket existente.
    evidence_subscription_id: str = "sub-evidence"
    evidence_gcs_bucket: str | None = None

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

    # takedown_agent.py (Sprint 6). Subscription propria sobre o topico
    # takedown-approved -- ja provisionada por infra/ (ver infra/README.md;
    # sub-takedown e o UNICO binding IAM de takedown-sa, garantia de
    # infraestrutura de que este processo so pode reagir a uma aprovacao ja
    # publicada, nunca iniciar uma acao por conta propria).
    takedown_subscription_id: str = "sub-takedown"

    # Topico Pub/Sub de aprovacao de takedown -- ja provisionado por
    # infra/main.tf (recurso `google_pubsub_topic.takedown_approved`, var
    # `takedown_topic_id`, mesmo default). Nao existia em `config.py` ate
    # aqui: so o dashboard (`dashboard/src/lib/gcp.ts::TAKEDOWN_TOPIC_ID`)
    # e o Terraform conheciam o nome; `agent_gateway.py` (Sprint 8, Parte
    # A) precisa dele em Python para rotear uma invocacao de
    # `takedown-agent` para o topico correto.
    takedown_topic_id: str = "takedown-approved"

    # Log de auditoria imutavel de cada execucao/rejeicao de takedown (ver
    # takedown_agent.py::_write_audit_record). Um documento novo por acao
    # (Firestore `.add()`), nunca atualizado -- historico, nao estado.
    takedown_actions_collection: str = "takedown_actions"
    # Contador de rate limit por marca/dia -- documento
    # `{marca}_{data-ISO}`, incrementado atomicamente numa transacao antes
    # de qualquer notificacao ser sequer preparada.
    takedown_rate_limit_collection: str = "takedown_rate_limits"
    takedown_daily_rate_limit_per_brand: int = 20

    # Endereco do time interno de Brand Security -- unico destino do canal
    # BRAND_SECURITY_TEAM (ver takedown_agent.py). Sem default deliberado:
    # e um endereco especifico da organizacao rodando o Sentinel, nao algo
    # que faca sentido chutar (mesma disciplina de GEMINI_MODEL_ID). Sem
    # configurar, o canal fica indisponivel -- rejeitado com log de
    # seguranca quando selecionado, nunca inventado.
    brand_security_team_email: str | None = None

    metrics_firestore_collection: str = "metrics"

    # Agent Registry (ver registry.py). Colecao central de manifestos --
    # publicar/descobrir agentes passa a ser dado em Firestore, nao import
    # hard-coded no orquestrador.
    agent_registry_collection: str = "agent_registry"

    # BrandAgent (Sprint 7, Parte A -- ver brand_agent.py). Contexto
    # operacional MUTAVEL por marca cliente (dominios legitimos, padroes de
    # typosquatting observados, contatos de abuso, tolerancia a risco,
    # limiar de escalonamento) -- distinto do AgentManifest publicado no
    # Agent Registry, que e o CONTRATO/versao (input/output schema), nao o
    # dado em si. Mesma separacao que ja existe entre "registry" (contrato)
    # e "investigations" (dado real gravado pelo orquestrador).
    brand_context_collection: str = "brand_context"

    # Memory Bank Adaptativo (Sprint 7, Parte B -- ver brand_memory.py).
    # Toda decisao humana terminal (rejeicao = falso positivo, aprovacao de
    # takedown = verdadeiro positivo) vira uma entrada aqui, injetada como
    # few-shot nas proximas investigacoes da mesma marca.
    brand_memory_collection: str = "brand_memory"
    # Quantos exemplos, no maximo, sao injetados por investigacao -- few-
    # shot aumenta tokens de ENTRADA (trabalha CONTRA a tese de token
    # economy do projeto, ver CLAUDE.md), entao isso e deliberadamente
    # configuravel: 0 desliga a injecao inteira, inclusive a leitura extra
    # no Firestore (ver brand_memory.get_relevant_memories).
    brand_memory_max_examples: int = 3

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

    # Agent Gateway (Sprint 8, Parte A -- ver agent_gateway.py). Ponto UNICO
    # de entrada HTTP para invocar qualquer agente do registry.
    #
    # Audience esperada nos ID tokens do Google verificados na etapa de
    # autenticacao (Agent Identity) -- deve ser a URL publica do proprio
    # servico depois do deploy (ver infra/README.md), para impedir que um
    # ID token emitido para OUTRO servico Cloud Run seja reaproveitado
    # aqui (confusao de audience). Sem default fixo por design (igual a
    # `gemini_model_id`/`brand_security_team_email`): nao existe um valor
    # generico correto antes do Cloud Run atribuir a URL. `None` (default
    # de desenvolvimento local) pula a checagem de audience -- a
    # assinatura/expiracao do token ainda sao verificadas -- e emite um
    # aviso alto no log de startup; NUNCA deixar `None` em producao.
    agent_gateway_audience: str | None = None

    # Log de auditoria imutavel de toda chamada a POST /invoke/{agent_id}
    # -- sucesso OU rejeicao em qualquer etapa do pipeline (autenticacao,
    # resolucao, schema, rate limit, autorizacao, roteamento). Mesmo
    # padrao de `takedown_actions_collection`: um documento novo por
    # chamada, nunca atualizado.
    agent_gateway_audit_log_collection: str = "agent_gateway_audit_log"

    # Rate limit por identidade chamadora + agente, contador transacional
    # por janela de minuto (mesmo mecanismo de
    # `takedown_rate_limit_collection`, mas com janela de minuto em vez de
    # dia -- o gateway protege contra abuso de chamada sincrona, nao
    # contra volume diario de notificacao externa).
    agent_gateway_rate_limit_collection: str = "agent_gateway_rate_limits"
    agent_gateway_rate_limit_per_minute: int = 30


settings = Settings()
