/**
 * Assinatura/verificação do cookie de sessão -- via Web Crypto
 * (`crypto.subtle`), não `node:crypto`. Motivo: este módulo é importado
 * por `middleware.ts`, que roda no Edge Runtime do Next.js (não é Node.js
 * puro) -- Web Crypto é a única API de assinatura garantida disponível
 * nos dois ambientes (Edge e Node.js do Route Handler), então é a única
 * escolha sem risco de quebrar um dos dois em produção.
 *
 * A verificação do ID token do Google (`google-auth-library`, que PRECISA
 * de módulos Node puros como `tls`/`net`) fica isolada em `session.ts`,
 * nunca importado por `middleware.ts` -- ver docstring lá.
 */

export const SESSION_COOKIE_NAME = "sentinel_session";
export const SESSION_TTL_SECONDS = 60 * 60 * 12; // 12h -- turno de revisão, não "logado pra sempre"

export interface ReviewerSession {
  email: string;
  name: string;
  picture: string | null;
  issuedAt: number;
  expiresAt: number;
}

function getSessionSecret(): string {
  const secret = process.env.SESSION_SECRET;
  if (!secret) {
    throw new Error(
      "SESSION_SECRET não definido -- gere com `openssl rand -base64 32` e " +
        "configure no .env (dev) ou no Cloud Run (deploy)."
    );
  }
  return secret;
}

let cachedKey: { secret: string; key: CryptoKey } | null = null;
async function getKey(): Promise<CryptoKey> {
  const secret = getSessionSecret();
  if (cachedKey && cachedKey.secret === secret) return cachedKey.key;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
  cachedKey = { secret, key };
  return key;
}

// Base64url via Web APIs puras (btoa/atob + TextEncoder/TextDecoder) -- de
// propósito, nada de `Buffer`: é global do Node.js, não garantido no Edge
// Runtime (mesmo motivo de usar Web Crypto em vez de `node:crypto` acima).
function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function sign(payload: string): Promise<string> {
  const key = await getKey();
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload));
  return bytesToBase64Url(new Uint8Array(signature));
}

/** Serializa + assina a sessão em `payload.signature` (base64url em ambos
 * os lados, cookie value plano, sem urlencode extra). */
export async function encodeSession(session: ReviewerSession): Promise<string> {
  const payload = bytesToBase64Url(new TextEncoder().encode(JSON.stringify(session)));
  return `${payload}.${await sign(payload)}`;
}

/** Decodifica e verifica a assinatura -- `null` se ausente, corrompido,
 * adulterado ou expirado (nunca lança: todo chamador trata "sem sessão"
 * como o caminho normal de "precisa logar"). */
export async function decodeSession(cookieValue: string | undefined): Promise<ReviewerSession | null> {
  if (!cookieValue) return null;
  const [payload, signature] = cookieValue.split(".");
  if (!payload || !signature) return null;

  const key = await getKey();
  const valid = await crypto.subtle.verify(
    "HMAC",
    key,
    base64UrlToBytes(signature),
    new TextEncoder().encode(payload)
  );
  if (!valid) return null;

  try {
    const session = JSON.parse(new TextDecoder().decode(base64UrlToBytes(payload))) as ReviewerSession;
    if (typeof session.expiresAt !== "number" || Date.now() / 1000 > session.expiresAt) return null;
    return session;
  } catch {
    return null;
  }
}
