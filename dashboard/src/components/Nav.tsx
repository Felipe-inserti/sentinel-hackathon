import Link from "next/link";
import { SignOutButton } from "./SignOutButton";
import type { ReviewerSession } from "@/lib/session";

const LINKS = [
  { href: "/review", label: "Fila de Revisão", icon: "🗂️" },
  { href: "/metrics", label: "Token Economy", icon: "📊" },
  { href: "/campaigns", label: "Campanhas", icon: "🕸️" },
];

export function Nav({ session, activePath }: { session: ReviewerSession; activePath: string }) {
  return (
    <header className="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-2">
          <span className="text-lg">🛡️</span>
          <span className="text-sm font-semibold tracking-tight text-zinc-100">Sentinel</span>
        </div>

        <nav className="flex items-center gap-1 overflow-x-auto">
          {LINKS.map((link) => {
            const active = activePath === link.href || activePath.startsWith(`${link.href}/`);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition ${
                  active
                    ? "bg-zinc-800 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                }`}
              >
                <span aria-hidden>{link.icon}</span>
                <span className="hidden sm:inline">{link.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-3">
          <div className="hidden items-center gap-2 sm:flex">
            {session.picture ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={session.picture} alt="" className="h-6 w-6 rounded-full" referrerPolicy="no-referrer" />
            ) : (
              <div className="h-6 w-6 rounded-full bg-zinc-700" />
            )}
            <span className="text-xs text-zinc-400">{session.email}</span>
          </div>
          <SignOutButton />
        </div>
      </div>
    </header>
  );
}
