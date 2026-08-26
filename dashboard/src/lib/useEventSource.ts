"use client";

import { useEffect, useRef, useState } from "react";

/** Consome um endpoint SSE (src/app/api/stream/*) via `EventSource` nativo
 * do navegador -- cookie de sessão vai junto automaticamente (mesma
 * origem). Reconecta sozinho (comportamento padrão do EventSource) se a
 * conexão cair. */
export function useEventSource<T>(url: string): { data: T | null; connected: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const source = new EventSource(url);
    sourceRef.current = source;

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data) as T);
      } catch {
        // Comentários SSE (heartbeat/": conectado") não têm `data:` --
        // EventSource já não dispara onmessage pra eles; se algo mal
        // formado chegar mesmo assim, ignora em vez de quebrar a UI.
      }
    };

    return () => source.close();
  }, [url]);

  return { data, connected };
}
