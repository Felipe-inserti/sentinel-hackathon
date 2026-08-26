"use client";

import { useState, useTransition } from "react";
import { approveTakedown, rejectInvestigation } from "@/app/(app)/review/actions";
import type { TakedownChannel } from "@/lib/types";

const CHANNEL_OPTIONS: { value: TakedownChannel; label: string }[] = [
  { value: "registrar_abuse", label: "Abuse do registrador" },
  { value: "hosting_abuse", label: "Abuse da hospedagem" },
  { value: "brand_protection_vendor", label: "Fornecedor de brand protection" },
];

type Mode = "closed" | "approve" | "reject";

export function DecisionForm({
  domain,
  suggestedChannel,
  onDone,
}: {
  domain: string;
  suggestedChannel?: TakedownChannel;
  onDone?: () => void;
}) {
  const [mode, setMode] = useState<Mode>("closed");
  const [channel, setChannel] = useState<TakedownChannel>(suggestedChannel ?? "registrar_abuse");
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  if (mode === "closed") {
    return (
      <div className="flex gap-2">
        <button
          onClick={() => setMode("approve")}
          className="flex-1 rounded-lg bg-emerald-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-emerald-500"
        >
          ✅ Aprovar Takedown
        </button>
        <button
          onClick={() => setMode("reject")}
          className="flex-1 rounded-lg bg-zinc-800 px-3 py-2 text-sm font-semibold text-zinc-200 transition hover:bg-zinc-700"
        >
          ✕ Rejeitar
        </button>
      </div>
    );
  }

  const submit = () => {
    setError(null);
    startTransition(async () => {
      const result =
        mode === "approve" ? await approveTakedown(domain, channel, text) : await rejectInvestigation(domain, text);
      if (!result.ok) {
        setError(result.error ?? "Falha inesperada");
        return;
      }
      setMode("closed");
      setText("");
      onDone?.();
    });
  };

  return (
    <div
      className={`rounded-lg border p-3 ${
        mode === "approve" ? "border-emerald-800/60 bg-emerald-950/20" : "border-zinc-700 bg-zinc-900/60"
      }`}
    >
      <p className="mb-2 text-xs font-semibold text-zinc-300">
        {mode === "approve" ? "Aprovar takedown de" : "Rejeitar investigação de"}{" "}
        <span className="font-mono text-zinc-100">{domain}</span>
      </p>

      {mode === "approve" && (
        <label className="mb-2 block text-xs text-zinc-400">
          Canal de notificação
          <select
            value={channel}
            onChange={(e) => setChannel(e.target.value as TakedownChannel)}
            className="mt-1 w-full rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200 focus:border-emerald-600 focus:outline-none"
          >
            {CHANNEL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="block text-xs text-zinc-400">
        {mode === "approve" ? "Justificativa da decisão" : "Motivo da rejeição"} (obrigatório)
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder={
            mode === "approve"
              ? "Ex: página clona o login do banco X, formulário de credenciais confirmado no screenshot, domínio registrado há 4h."
              : "Ex: falso positivo -- domínio de parceiro legítimo, marca reaproveitada com autorização."
          }
          className="mt-1 w-full resize-none rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-zinc-500 focus:outline-none"
        />
      </label>

      {error && <p className="mt-2 text-xs text-rose-400">{error}</p>}

      <div className="mt-3 flex gap-2">
        <button
          onClick={submit}
          disabled={pending || text.trim().length < 10}
          className={`flex-1 rounded-md px-3 py-1.5 text-sm font-semibold text-white transition disabled:cursor-not-allowed disabled:opacity-40 ${
            mode === "approve" ? "bg-emerald-600 hover:bg-emerald-500" : "bg-rose-600 hover:bg-rose-500"
          }`}
        >
          {pending ? "Enviando…" : mode === "approve" ? "Confirmar aprovação" : "Confirmar rejeição"}
        </button>
        <button
          onClick={() => {
            setMode("closed");
            setError(null);
          }}
          disabled={pending}
          className="rounded-md px-3 py-1.5 text-sm font-medium text-zinc-400 transition hover:text-zinc-200"
        >
          Cancelar
        </button>
      </div>
    </div>
  );
}
