export function StatTile({
  label,
  value,
  sub,
  tone = "zinc",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "zinc" | "rose" | "emerald" | "amber";
}) {
  const toneClass = {
    zinc: "text-zinc-100",
    rose: "text-rose-400",
    emerald: "text-emerald-400",
    amber: "text-amber-400",
  }[tone];

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</p>
      <p className={`mt-1 font-mono text-2xl font-semibold ${toneClass}`}>{value}</p>
      {sub && <p className="mt-1 text-xs text-zinc-600">{sub}</p>}
    </div>
  );
}
