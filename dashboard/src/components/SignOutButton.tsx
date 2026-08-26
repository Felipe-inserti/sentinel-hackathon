"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function SignOutButton() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);

  return (
    <button
      onClick={async () => {
        setLoading(true);
        await fetch("/api/session", { method: "DELETE" });
        router.replace("/login");
        router.refresh();
      }}
      disabled={loading}
      className="rounded-lg px-3 py-1.5 text-xs font-medium text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50"
    >
      {loading ? "Saindo…" : "Sair"}
    </button>
  );
}
