import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { cx } from "./ui";

const NAV = [
  { to: "/events", label: "Events" },
  { to: "/trace", label: "Trace" },
  { to: "/settings", label: "Settings" },
];

export default function Layout() {
  const { email } = useAuth();
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-slate-50 md:flex">
        <div className="flex items-center gap-2 px-5 py-5">
          <img src="/favicon.svg" alt="" className="h-6 w-6" />
          <span className="text-base font-semibold tracking-tight">Paytriq</span>
        </div>
        <nav className="flex flex-col gap-0.5 px-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cx(
                  "rounded-md px-3 py-2 text-sm transition-colors",
                  isActive ? "bg-white font-medium text-indigo-700 shadow-sm ring-1 ring-slate-200" : "text-slate-600 hover:bg-slate-100",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto truncate px-5 py-4 text-xs text-slate-500">{email}</div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 border-b border-slate-200 px-4 py-3 md:hidden">
          <span className="font-semibold">Paytriq</span>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={({ isActive }) => cx("text-sm", isActive ? "text-indigo-700" : "text-slate-600")}>
              {item.label}
            </NavLink>
          ))}
        </header>
        <main className="mx-auto w-full max-w-[1100px] px-4 py-8 md:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
