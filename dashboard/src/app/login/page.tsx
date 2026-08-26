"use client";

import Script from "next/script";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

// Tipagem mínima do que usamos da API global do Google Identity Services
// (carregada via <Script> abaixo) -- a lib não publica tipos próprios.
interface GoogleCredentialResponse {
  credential: string;
}
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (resp: GoogleCredentialResponse) => void;
          }) => void;
          renderButton: (parent: HTMLElement, options: Record<string, unknown>) => void;
        };
      };
    };
  }
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const buttonRef = useRef<HTMLDivElement>(null);
  const [scriptReady, setScriptReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const clientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;

  const handleCredential = useCallback(
    async (resp: GoogleCredentialResponse) => {
      setLoading(true);
      setError(null);
      try {
        const apiResp = await fetch("/api/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ idToken: resp.credential }),
        });
        if (!apiResp.ok) {
          const data = await apiResp.json().catch(() => ({}));
          throw new Error(data.error ?? "Falha ao autenticar");
        }
        router.replace(searchParams.get("next") || "/review");
        router.refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Falha ao autenticar");
        setLoading(false);
      }
    },
    [router, searchParams]
  );

  useEffect(() => {
    if (!scriptReady || !clientId || !buttonRef.current || !window.google) return;
    window.google.accounts.id.initialize({ client_id: clientId, callback: handleCredential });
    window.google.accounts.id.renderButton(buttonRef.current, {
      theme: "filled_black",
      size: "large",
      shape: "pill",
      text: "signin_with",
      width: 280,
    });
  }, [scriptReady, clientId, handleCredential]);

  return (
    <div className="flex min-h-screen flex-1 items-center justify-center bg-zinc-950 px-4">
      <Script
        src="https://accounts.google.com/gsi/client"
        strategy="afterInteractive"
        onReady={() => setScriptReady(true)}
      />
      <div className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900/60 p-8 text-center shadow-2xl">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-rose-500/10 text-2xl">
          🛡️
        </div>
        <h1 className="text-lg font-semibold text-zinc-100">Sentinel</h1>
        <p className="mt-1 mb-6 text-sm text-zinc-500">
          Fila de revisão humana &mdash; entre com sua conta Google para aprovar ou rejeitar takedowns.
        </p>

        {!clientId ? (
          <p className="rounded-lg border border-amber-800/50 bg-amber-950/40 p-3 text-xs text-amber-300">
            NEXT_PUBLIC_GOOGLE_CLIENT_ID não configurado. Ver dashboard/README.md.
          </p>
        ) : (
          <div className="flex flex-col items-center gap-3">
            <div ref={buttonRef} />
            {loading && <p className="text-xs text-zinc-500">Entrando…</p>}
            {error && (
              <p className="rounded-lg border border-rose-800/50 bg-rose-950/40 p-3 text-xs text-rose-300">
                {error}
              </p>
            )}
          </div>
        )}

        <p className="mt-8 text-[11px] leading-relaxed text-zinc-600">
          Toda aprovação/rejeição fica registrada com o e-mail Google verificado nesta sessão
          &mdash; requisito de auditoria da trilha.
        </p>
      </div>
    </div>
  );
}
