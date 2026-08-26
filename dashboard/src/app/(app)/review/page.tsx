"use client";

import { useEventSource } from "@/lib/useEventSource";
import { ReviewCard } from "@/components/ReviewCard";
import { EmptyState } from "@/components/EmptyState";
import type { Investigation } from "@/lib/types";

interface QueueEvent {
  items?: Investigation[];
  error?: string;
}

export default function ReviewQueuePage() {
  const { data, connected } = useEventSource<QueueEvent>("/api/stream/queue");

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Fila de Revisão</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Domínios classificados <span className="text-rose-400">MALICIOUS</span> com evidência coletada,
            ordenados por confiança decrescente.
          </p>
        </div>
        <ConnectionIndicator connected={connected} />
      </div>

      {data?.error && (
        <div className="mb-4 rounded-lg border border-rose-800/50 bg-rose-950/30 p-3 text-sm text-rose-300">
          Erro ao carregar a fila: {data.error}
        </div>
      )}

      {!data ? (
        <SkeletonGrid />
      ) : data.items && data.items.length > 0 ? (
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
          {data.items.map((item) => (
            <ReviewCard key={item.domain} investigation={item} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon="🛡️"
          title="Fila vazia"
          description="Nenhum domínio malicioso aguardando revisão no momento. Novos itens aparecem aqui automaticamente assim que o evidence-collector terminar a coleta."
        />
      )}
    </div>
  );
}

function ConnectionIndicator({ connected }: { connected: boolean }) {
  return (
    <span className="flex items-center gap-1.5 text-xs text-zinc-500">
      <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-500" : "bg-zinc-600"}`} />
      {connected ? "tempo real" : "conectando…"}
    </span>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="h-96 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/40" />
      ))}
    </div>
  );
}
