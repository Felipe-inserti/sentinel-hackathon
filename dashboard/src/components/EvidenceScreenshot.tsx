"use client";

import { useState } from "react";
import type { ArtifactRef, FormFieldSignal } from "@/lib/types";

/** Screenshot da página de phishing. Imagem não dá pra redigir PII por
 * regex (ver docstring de evidence_agent.py) -- quando
 * `form_fields_detected.detected` for true, o sinal prescrito pelo sprint
 * é DETECTAR e SINALIZAR, não redigir; aqui isso vira blur por padrão +
 * revelar sob ação explícita do revisor, pra "nenhum PII renderizado em
 * tela" valer também pra esse único artefato que não passa pelo
 * sanitizer.py. */
export function EvidenceScreenshot({
  screenshot,
  formSignal,
  domain,
}: {
  screenshot: ArtifactRef;
  formSignal: FormFieldSignal;
  domain: string;
}) {
  const [revealed, setRevealed] = useState(!formSignal.detected);
  const src = `/api/artifact?uri=${encodeURIComponent(screenshot.gcs_uri)}`;

  return (
    <div className="relative overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={`Screenshot de ${domain}`}
        className={`block w-full object-cover object-top transition ${
          revealed ? "" : "scale-105 blur-xl"
        }`}
        loading="lazy"
      />
      {!revealed && (
        <button
          onClick={() => setRevealed(true)}
          className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-zinc-950/70 text-center text-xs font-medium text-zinc-200 transition hover:bg-zinc-950/60"
        >
          <span className="text-lg">🙈</span>
          <span className="max-w-[80%]">
            Formulário preenchido detectado ({formSignal.field_count} campo
            {formSignal.field_count === 1 ? "" : "s"}) &mdash; pode conter dado de vítima
          </span>
          <span className="rounded-full bg-zinc-100 px-3 py-1 text-[11px] font-semibold text-zinc-900">
            Revelar mesmo assim
          </span>
        </button>
      )}
    </div>
  );
}
