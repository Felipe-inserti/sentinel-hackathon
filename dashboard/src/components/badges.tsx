function cx(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

export function ConfidenceBadge({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const tone =
    confidence >= 0.9
      ? "bg-rose-500/15 text-rose-300 ring-rose-500/30"
      : confidence >= 0.75
        ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
        : "bg-zinc-500/15 text-zinc-300 ring-zinc-500/30";
  return (
    <span className={cx("inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold ring-1", tone)}>
      {pct}% confiança
    </span>
  );
}

/** Idade do domínio -- sinal de destaque explícito no dossiê (RDAP
 * `domain_age_hours`, ver evidence_agent.py): domínio criado há poucas
 * horas é sinal forte de phishing. */
export function DomainAgeBadge({ hours }: { hours: number | null | undefined }) {
  if (hours == null) {
    return (
      <span className="inline-flex items-center rounded-full bg-zinc-800 px-2.5 py-1 text-xs font-medium text-zinc-500 ring-1 ring-zinc-700">
        idade do domínio: desconhecida
      </span>
    );
  }

  const label = formatAge(hours);
  const tone =
    hours < 24
      ? "bg-rose-500/15 text-rose-300 ring-rose-500/30"
      : hours < 24 * 7
        ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
        : "bg-emerald-500/10 text-emerald-300 ring-emerald-500/20";

  return (
    <span className={cx("inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ring-1", tone)}>
      🕒 domínio criado há {label}
    </span>
  );
}

function formatAge(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)}min`;
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

export function InjectionSignalBadge({ signal }: { signal: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-violet-500/15 px-2 py-0.5 text-[11px] font-medium text-violet-300 ring-1 ring-violet-500/30">
      ⚠︎ {signal}
    </span>
  );
}

export function StatusBadge({ status }: { status?: string }) {
  if (!status) return null;
  const map: Record<string, string> = {
    PENDING_HUMAN_REVIEW: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    TAKEDOWN_APPROVED: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    REJECTED: "bg-zinc-700/40 text-zinc-400 ring-zinc-600/40",
  };
  const label: Record<string, string> = {
    PENDING_HUMAN_REVIEW: "Aguardando revisão",
    TAKEDOWN_APPROVED: "Takedown aprovado",
    REJECTED: "Rejeitado",
  };
  return (
    <span
      className={cx(
        "inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold ring-1",
        map[status] ?? "bg-zinc-800 text-zinc-400 ring-zinc-700"
      )}
    >
      {label[status] ?? status}
    </span>
  );
}

export function Hash({ value, length = 12 }: { value: string; length?: number }) {
  return (
    <code className="rounded bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400" title={value}>
      {value.slice(0, length)}…
    </code>
  );
}
