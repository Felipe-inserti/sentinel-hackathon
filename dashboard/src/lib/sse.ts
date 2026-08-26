import "server-only";

/**
 * Helper de Server-Sent Events para os Route Handlers de streaming
 * (src/app/api/stream/*). Não depende de nenhum SDK client-side do
 * Firebase -- o listener real (`onSnapshot`) roda aqui no servidor, via
 * `@google-cloud/firestore` (Admin), e cada mudança é reempurrada pro
 * navegador como um evento SSE. Ver decisão registrada em
 * src/lib/session.ts sobre por que não há Firestore client-side aqui.
 */
export function sseResponse(
  subscribe: (send: (data: unknown) => void, signal: AbortSignal) => () => void
): Response {
  const encoder = new TextEncoder();
  let unsubscribe: (() => void) | null = null;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (data: unknown) => {
        try {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`));
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
