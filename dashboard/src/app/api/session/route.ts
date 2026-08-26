import { NextRequest, NextResponse } from "next/server";
import { encodeSession, SESSION_COOKIE_NAME, verifyGoogleIdToken } from "@/lib/session";

/** Troca o ID token do Google Identity Services por um cookie de sessão
 * httpOnly assinado (ver src/lib/session.ts). Chamado pelo botão "Sign In
 * With Google" em /login. */
export async function POST(request: NextRequest) {
  const body = (await request.json().catch(() => null)) as { idToken?: string } | null;
  if (!body?.idToken) {
    return NextResponse.json({ error: "idToken ausente" }, { status: 400 });
  }

  let session;
  try {
    session = await verifyGoogleIdToken(body.idToken);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Token inválido" },
      { status: 401 }
    );
  }

  const response = NextResponse.json({ email: session.email, name: session.name });
  response.cookies.set(SESSION_COOKIE_NAME, await encodeSession(session), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: session.expiresAt - session.issuedAt,
  });
  return response;
}

/** Sign out -- limpa o cookie. */
export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE_NAME, "", { path: "/", maxAge: 0 });
  return response;
}
