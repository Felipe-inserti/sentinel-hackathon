"""Cliente unico de LLM do Sentinel (Vertex AI via `google-genai`).

Nenhum outro modulo deve importar `google.genai` diretamente. Toda chamada
ao modelo passa por `LLMClient.generate`, que:

  1. Mantem `system_prompt` (instrucao confiavel) e `untrusted_data` (ex:
     texto raspado de um site suspeito, ja sanitizado e delimitado com
     nonce por `sanitizer.wrap_untrusted_content`) como turnos separados
     na chamada -- nunca concatenados na mesma string -- para reduzir a
     superficie de prompt injection vinda de conteudo adversarial (regra
     de seguranca do projeto: conteudo raspado e dado, nunca instrucao).
     Este modulo NAO adiciona wrapping/delimitador proprio -- isso e
     responsabilidade exclusiva do chamador via `sanitizer.py`.
  2. Retenta erros transitorios (5xx e 429) com backoff exponencial.
  3. Retenta ate `MAX_SCHEMA_RETRIES` vezes quando a saida do modelo nao
     valida contra o schema Pydantic esperado.
  4. Sempre devolve metadados de uso (tokens de entrada/saida, latencia,
     ID do modelo) junto com o dado validado, e loga cada chamada.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from config import settings

logger = logging.getLogger("llm_client")

T = TypeVar("T", bound=BaseModel)

MAX_TRANSIENT_RETRIES = 3
MAX_SCHEMA_RETRIES = 2
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 16.0

# 429 (RESOURCE_EXHAUSTED / rate limit) e o unico erro 4xx que vale retentar;
# os demais (400, 403, 404...) sao erros de requisicao e retentar so atrasa
# a falha.
_RETRYABLE_CLIENT_STATUS_CODES = frozenset({429})


@dataclass(frozen=True)
class LLMUsage:
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    data: T
    usage: LLMUsage


class LLMSchemaValidationError(RuntimeError):
    """Levantado quando o modelo nao produz saida valida para o schema
    pedido mesmo apos as retentativas."""


class LLMClient:
    """Wrapper fino sobre `google.genai.Client` (modo Vertex AI)."""

    def __init__(self) -> None:
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )

    async def generate(
        self,
        system_prompt: str,
        untrusted_data: str,
        response_schema: type[T],
        *,
        temperature: float = 0.1,
    ) -> LLMResult[T]:
        """Classifica `untrusted_data` seguindo `system_prompt`, devolvendo
        uma instancia validada de `response_schema` mais os metadados de
        uso da chamada.

        `untrusted_data` e sempre enviado como turno de usuario separado da
        instrucao de sistema -- o chamador nao deve fazer f-string/concat
        manual do dado raspado dentro do `system_prompt`.

        MUDANCA DE CONTRATO: `untrusted_data` deve chegar aqui ja totalmente
        delimitado pelo chamador (ver `sanitizer.wrap_untrusted_content`),
        incluindo o nonce aleatorio por requisicao embutido nas tags de
        isolamento. Este metodo NAO adiciona nenhum wrapping estatico
        proprio -- um delimitador fixo e previsivel seria um segundo alvo
        de escape que um atacante poderia mirar mesmo sem conhecer o nonce
        dinamico. `generate()` repassa `untrusted_data` verbatim como
        `contents` da chamada ao modelo.
        """
        config = genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        schema_attempt = 0
        while True:
            start = time.monotonic()
            response = await self._call_with_transient_retry(untrusted_data, config)
            latency_ms = (time.monotonic() - start) * 1000

            usage = self._extract_usage(latency_ms, response)
            logger.info(
                "llm_call model=%s input_tokens=%d output_tokens=%d latency_ms=%.1f",
                usage.model_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.latency_ms,
            )

            parsed = self._parse_response(response, response_schema)
            if parsed is not None:
                return LLMResult(data=parsed, usage=usage)

            schema_attempt += 1
            if schema_attempt > MAX_SCHEMA_RETRIES:
                raise LLMSchemaValidationError(
                    f"Gemini nao retornou saida valida para "
                    f"{response_schema.__name__} apos {schema_attempt} tentativas"
                )
            logger.warning(
                "Saida do Gemini nao validou contra %s, retentando (%d/%d)",
                response_schema.__name__,
                schema_attempt,
                MAX_SCHEMA_RETRIES,
            )

    async def _call_with_transient_retry(
        self, contents: str, config: genai_types.GenerateContentConfig
    ) -> genai_types.GenerateContentResponse:
        attempt = 0
        while True:
            try:
                return await self._client.aio.models.generate_content(
                    model=settings.gemini_model_id,
                    contents=contents,
                    config=config,
                )
            except genai_errors.ServerError as exc:
                attempt = await self._sleep_backoff_or_raise(attempt, exc)
            except genai_errors.ClientError as exc:
                if exc.code not in _RETRYABLE_CLIENT_STATUS_CODES:
                    raise
                attempt = await self._sleep_backoff_or_raise(attempt, exc)

    @staticmethod
    async def _sleep_backoff_or_raise(attempt: int, exc: Exception) -> int:
        attempt += 1
        if attempt > MAX_TRANSIENT_RETRIES:
            logger.error(
                "Erro transitorio persistente apos %d tentativas: %s", attempt - 1, exc
            )
            raise exc
        delay = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
        logger.warning(
            "Erro transitorio na chamada ao Gemini (%s), retry %d/%d em %.1fs",
            exc,
            attempt,
            MAX_TRANSIENT_RETRIES,
            delay,
        )
        await asyncio.sleep(delay)
        return attempt

    @staticmethod
    def _parse_response(
        response: genai_types.GenerateContentResponse, schema: type[T]
    ) -> T | None:
        if isinstance(response.parsed, BaseModel):
            return response.parsed
        if not response.text:
            return None
        try:
            return schema.model_validate_json(response.text)
        except ValidationError:
            return None

    @staticmethod
    def _extract_usage(
        latency_ms: float, response: genai_types.GenerateContentResponse
    ) -> LLMUsage:
        usage_metadata = response.usage_metadata
        return LLMUsage(
            model_id=settings.gemini_model_id,
            input_tokens=(usage_metadata.prompt_token_count or 0) if usage_metadata else 0,
            output_tokens=(usage_metadata.candidates_token_count or 0) if usage_metadata else 0,
            latency_ms=latency_ms,
        )


llm_client = LLMClient()
