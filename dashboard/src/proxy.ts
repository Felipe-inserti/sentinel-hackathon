import { NextRequest, NextResponse } from "next/server";
import { decodeSession, SESSION_COOKIE_NAME } from "@/lib/session-cookie";

// Rotas acessíveis sem sessão: a própria página de login, a troca de
// token->cookie, e assets estáticos do Next (matcher abaixo já exclui
// _next/estático, mas login+api/session precisam de exceção explícita).
const PUBLIC_PATHS = ["/login", "/api/session"];

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return NextResponse.next();
  }

  const session = await decodeSession(request.cookies.get(SESSION_COOKIE_NAME)?.value);
  if (!session) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", pathname);
    return NextResponse.redirect(loginUrl);
  }

  // Repassa o pathname atual num header pra Server Components (ex:
  // src/app/(app)/layout.tsx) saberem qual link do Nav marcar como ativo
  // sem precisar de um hook client-side -- usePathname() só existe em
  // Client Component, e o Nav é renderizado no layout server.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-pathname", pathname);
  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = {
  // Tudo exceto arquivos estáticos do Next e o favicon -- inclui as rotas
  // /api/stream/* (SSE) de propósito: sem sessão válida, sem stream.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
