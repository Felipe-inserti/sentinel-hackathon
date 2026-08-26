export function EmptyState({
  icon,
  title,
  description,
}: {
  icon: string;
  title: string;
  description: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-800 bg-zinc-900/30 px-6 py-20 text-center">
      <div className="mb-4 text-4xl">{icon}</div>
      <h2 className="text-base font-semibold text-zinc-200">{title}</h2>
      <p className="mt-2 max-w-sm text-sm text-zinc-500">{description}</p>
    </div>
  );
}
