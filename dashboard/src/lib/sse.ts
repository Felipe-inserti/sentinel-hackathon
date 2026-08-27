import "server-only";
import { Timestamp } from "@google-cloud/firestore";

/**
 * Helper de Server-Sent Events para os Route Handlers de streaming
 * (src/app/api/stream/*). Não depende de nenhum SDK client-side do
 * Firebase -- o listener real (`onSnapshot`) roda aqui no servidor, via
 * `@google-cloud/firestore` (Admin), e cada mudança é reempurrada pro
 * navegador como um evento SSE. Ver decisão registrada em
 * src/lib/session.ts sobre por que não há Firestore client-side aqui.
 */

/**
 * Achado da verificação ponta a ponta (27/08): `orchestrator.py` grava
 * `investigated_at` como `datetime` Python cru (não `.isoformat()` via
 * Pydantic `model_dump(mode="json")`, como `evidence_agent.py` faz pra
 * `collected_at`) -- o Admin SDK do Node devolve isso como um `Timestamp`
 * do Firestore, que não tem `toJSON()`. `JSON.stringify` direto nele vira
 * `{"_seconds":...,"_nanoseconds":...}`, e todo `new Date(...)` do lado
 * cliente (Timeline.tsx, CloudTraceLink.tsx) virava "Invalid Date" --
 * confirmado rodando contra um dossiê real de produção antes deste fix.
 *
 * Normaliza QUALQUER `Timestamp` em qualquer profundidade do payload pra
 * string ISO 8601, aqui no único ponto por onde os dois streams
 * (/api/stream/queue, /api/stream/investigation/[domain]) passam --
 * cobre `investigated_at` hoje e qualquer campo Timestamp futuro, sem
 * precisar mudar `orchestrator.py` nem fazer backfill dos documentos já
 * gravados (a conversão acontece na leitura, não na escrita).
 */
function normalizeTimestamps(value: unknown): unknown {
  if (value instanceof Timestamp) {
    return value.toDate().toISOString();
  }
  if (Array.isArray(value)) {
    return value.map(normalizeTimestamps);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, v]) => [key, normalizeTimestamps(v)])
    );
  }
  return value;
}

export function sseResponse(
  subscribe: (send: (data: unknown) => void, signal: AbortSignal) => () => void
): Response {
  const encoder = new TextEncoder();
  let unsubscribe: (() => void) | null = null;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (data: unknown) => {
        try {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(normalizeTimestamps(data))}\n\n`));
        } catch {
          // Controller já fechado (cliente desconectou entre o snapshot
          // chegar e o enqueue rodar) -- ignora, o cleanup abaixo já cuida.
        }
      };
      // Comentário SSE inicial só pra abrir a conexão imediatamente (alguns
      // proxies/browsers seguram o primeiro byte até algo ser escrito).
      controller.enqueue(encoder.encode(": conectado\n\n"));

      const controllerAbort = new AbortController();
      unsubscribe = subscribe(send, controllerAbort.signal);

      const heartbeat = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(": ping\n\n"));
        } catch {
          clearInterval(heartbeat);
        }
      }, 25_000);

      controllerAbort.signal.addEventListener("abort", () => clearInterval(heartbeat));
    },
    cancel() {
      unsubscribe?.();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no", // desliga buffering de proxies tipo nginx
    },
  });
}
