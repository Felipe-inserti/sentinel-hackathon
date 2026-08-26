/**
 * Sessão do revisor humano -- SEM Firebase Auth.
 *
 * Por quê: "Autenticação com Google Identity" foi implementada com o
 * widget "Sign In With Google" do Google Identity Services (GIS) --
 * `https://accounts.google.com/gsi/client`, só o Client ID público, sem
 * client secret. O ID token que ele devolve é verificado aqui no servidor
 * com `google-auth-library` (mesma biblioteca oficial usada por qualquer
 * backend Google) contra as chaves públicas do Google -- a prova
 * criptográfica de identidade não depende de nenhum SDK do Firebase.
 *
 * Firebase Auth foi avaliado e descartado: a API de Management do Firebase
 * (`addFirebase`) recusou a chamada com 403 mesmo com `roles/owner` no
 * projeto -- indício de um aceite de Termos de Serviço pendente, só
 * possível pelo console, que não pude contornar por API/CLI. GIS não tem
 * essa dependência: só exige UM OAuth Client ID (`GOOGLE_CLIENT_ID`),
 * criado uma vez no console (ver dashboard/README.md).
 *
 * Este módulo importa `google-auth-library`, que precisa de módulos
 * Node.js puros (`tls`/`net`) -- por isso NUNCA é importado por
 * `middleware.ts` (Edge Runtime, sem esses módulos). O cookie em si
 * (assinar/verificar/ler) é `session-cookie.ts`, Edge-safe via Web
 * Crypto, importado tanto aqui quanto no middleware.
 */

import "server-only";
import { cookies } from "next/headers";
import { OAuth2Client } from "google-auth-library";
import {
  decodeSession as decodeSessionCookie,
  SESSION_COOKIE_NAME,
  SESSION_TTL_SECONDS,
  type ReviewerSession,
} from "@/lib/session-cookie";

export { SESSION_COOKIE_NAME, encodeSession, decodeSession } from "@/lib/session-cookie";
export type { ReviewerSession } from "@/lib/session-cookie";

/** Lê a sessão da request atual (Server Component/Action/Route Handler).
 * `null` se não autenticado -- chamadores decidem o que fazer (a UI de
 * página confia no `middleware.ts` já ter redirecionado para /login; as
 * Server Actions RE-VERIFICAM aqui mesmo assim, nunca confiam só no
 * middleware para uma ação que grava dado -- ver review/actions.ts). */
export async function getSession(): Promise<ReviewerSession | null> {
  const store = await cookies();
  return decodeSessionCookie(store.get(SESSION_COOKIE_NAME)?.value);
}

function getGoogleClientId(): string {
  const clientId = process.env.GOOGLE_CLIENT_ID;
  if (!clientId) {
    throw new Error(
      "GOOGLE_CLIENT_ID não definido -- ver dashboard/README.md para criar o OAuth Client ID."
    );
  }
  return clientId;
}

const oauthClient = new OAuth2Client();

/** Verifica o ID token do GIS (assinatura, `aud`, `iss`, expiração --
 * tudo feito por `google-auth-library` contra as chaves públicas do
 * Google) e devolve a sessão pronta para `encodeSession`. Lança se o
 * token for inválido -- o chamador (rota /api/session) responde 401. */
export async function verifyGoogleIdToken(idToken: string): Promise<ReviewerSession> {
  const clientId = getGoogleClientId();
  const ticket = await oauthClient.verifyIdToken({ idToken, audience: clientId });
  const payload = ticket.getPayload();
  if (!payload?.email) {
    throw new Error("Token do Google sem e-mail no payload");
  }
  if (payload.email_verified === false) {
    throw new Error("E-mail do Google não verificado");
  }

  assertEmailAllowed(payload.email);

  const now = Math.floor(Date.now() / 1000);
  return {
    email: payload.email,
    name: payload.name ?? payload.email,
    picture: payload.picture ?? null,
    issuedAt: now,
    expiresAt: now + SESSION_TTL_SECONDS,
  };
}

/** Allowlist opcional (`ALLOWED_REVIEWER_DOMAIN` e/ou
 * `ALLOWED_REVIEWER_EMAILS`, separados por vírgula) -- sem nenhuma das
 * duas configuradas, qualquer conta Google verificada entra (aceitável
 * para demo/hackathon; documentado no README como o primeiro endurecimento
 * a fazer antes de um uso real). Auditoria (regra da trilha) já não
 * depende disso: `approved_by` sempre é o e-mail verificado pelo Google,
 * nunca um valor que o cliente possa forjar. */
function assertEmailAllowed(email: string): void {
  const allowedEmails = (process.env.ALLOWED_REVIEWER_EMAILS ?? "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
  const allowedDomain = (process.env.ALLOWED_REVIEWER_DOMAIN ?? "").trim().toLowerCase();

  if (allowedEmails.length === 0 && !allowedDomain) return;

  const normalized = email.toLowerCase();
  const domainOk = allowedDomain && normalized.endsWith(`@${allowedDomain}`);
  const emailOk = allowedEmails.includes(normalized);
  if (!domainOk && !emailOk) {
    throw new Error(`E-mail ${email} não está na allowlist de revisores`);
  }
}
