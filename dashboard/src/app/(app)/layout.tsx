import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { getSession } from "@/lib/session";
import { Nav } from "@/components/Nav";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  // O middleware já bloqueia requests sem sessão antes de chegar aqui;
  // esta checagem é defensiva (nunca confiar só em uma camada), não o
  // gate principal.
  const session = await getSession();
  if (!session) redirect("/login");

  const pathname = (await headers()).get("x-pathname") ?? "/review";

  return (
    <div className="flex min-h-screen flex-col">
      <Nav session={session} activePath={pathname} />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">{children}</main>
    </div>
  );
}
