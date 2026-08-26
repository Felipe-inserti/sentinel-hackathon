import type { Investigation } from "@/lib/types";

interface TimelineEvent {
  label: string;
  detail?: string;
  at: string;
  tone: "zinc" | "amber" | "emerald" | "rose";
}

/** Monta a timeline só a partir de timestamps que o pipeline já persiste
 * (`investigated_at`, `evidence.collected_at`, `approved_at`/`rejected_at`)
 * -- nenhum evento novo é inventado/gravado, isso é derivado do dossiê
 * existente. */
export function buildTimeline(inv: Investigation): TimelineEvent[] {
  const events: TimelineEvent[] = [
    {
      label: "Investigação concluída",
      detail: `Classificado ${inv.classification} pelo Gemini (${inv.model}), confiança ${(inv.confidence * 100).toFixed(0)}%`,
      at: inv.investigated_at,
      tone: inv.classification === "MALICIOUS" ? "rose" : "zinc",
    },
  ];

  if (inv.evidence) {
    events.push({
      label: "Evidência coletada",
      detail: inv.evidence.is_partial
        ? `Bundle parcial (${inv.evidence.collection_errors.length} etapa(s) falharam)`
        : "Bundle completo, chain of custody fechada",
      at: inv.evidence.collected_at,
      tone: "amber",
    });
  }

  if (inv.approved_at) {
    events.push({
      label: "Takedown aprovado",
      detail: `por ${inv.approved_by} · canal ${inv.takedown_channel}`,
      at: inv.approved_at,
      tone: "emerald",
    });
  }
  if (inv.rejected_at) {
    events.push({
      label: "Rejeitado",
      detail: `por ${inv.rejected_by}`,
      at: inv.rejected_at,
      tone: "zinc",
    });
  }

  return events.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

const DOT_TONE: Record<TimelineEvent["tone"], string> = {
  zinc: "bg-zinc-500",
  amber: "bg-amber-500",
  emerald: "bg-emerald-500",
  rose: "bg-rose-500",
};

export function Timeline({ events }: { events: TimelineEvent[] }) {
  return (
    <ol className="relative ml-2 border-l border-zinc-800 pl-5">
      {events.map((event, i) => (
        <li key={i} className="mb-5 last:mb-0">
          <span className={`absolute -ml-[25px] mt-1 h-2.5 w-2.5 rounded-full ring-4 ring-zinc-950 ${DOT_TONE[event.tone]}`} />
          <p className="text-sm font-medium text-zinc-200">{event.label}</p>
          {event.detail && <p className="text-xs text-zinc-500">{event.detail}</p>}
          <time className="text-[11px] text-zinc-600">{new Date(event.at).toLocaleString("pt-BR")}</time>
        </li>
      ))}
    </ol>
  );
}
