import { MessageSquareText, Moon, PanelLeftClose, PanelLeftOpen, Settings, Sun, Waypoints } from "lucide-react";
import { useState } from "react";
import type { Status } from "../types";
import { isDark, toggleTheme } from "../lib/theme";
import { IconButton } from "./ui";

export function TopBar({
  status,
  collapsed,
  view,
  onMenu,
  onSettings,
  onView,
}: {
  status: Status | null;
  collapsed: boolean;
  view: "chat" | "graph";
  onMenu: () => void;
  onSettings: () => void;
  onView: (view: "chat" | "graph") => void;
}) {
  const [dark, setDark] = useState(isDark());
  return (
    <header className="z-10 flex h-[62px] flex-shrink-0 items-center gap-3 px-5 md:px-6">
      <IconButton aria-label="Toggle sidebar" title="Toggle sidebar (Ctrl+B)" onClick={onMenu}>
        {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
      </IconButton>
      <span className="flex items-center gap-2.5 font-display text-[17px] font-semibold tracking-tight">
        <span className="relative flex h-[10px] w-[10px]">
          <span className="absolute inset-0 animate-ping2 rounded-full bg-accent" />
          <span className="relative h-[10px] w-[10px] rounded-full bg-accent" />
        </span>
        QuickJoiner
      </span>
      {status && (
        <div className="hidden items-center gap-1.5 pl-2 sm:flex">
          <span className="rounded-full bg-fill px-3 py-1 text-[12px] font-medium text-muted">
            {status.org}
          </span>
          <span className="rounded-full bg-accent-soft px-3 py-1 font-mono text-[11px] text-accent">
            {status.llm.provider} · {status.llm.model}
          </span>
        </div>
      )}
      <div className="ml-auto flex items-center gap-1">
        <IconButton
          aria-label={view === "graph" ? "Back to chat" : "Knowledge graph"}
          title={view === "graph" ? "Back to chat" : "Knowledge graph — what qj has learned"}
          onClick={() => onView(view === "graph" ? "chat" : "graph")}
        >
          {view === "graph" ? <MessageSquareText size={17} /> : <Waypoints size={17} />}
        </IconButton>
        <IconButton
          aria-label="Toggle theme"
          title="Toggle light/dark"
          onClick={() => setDark(toggleTheme())}
        >
          {dark ? <Sun size={17} /> : <Moon size={17} />}
        </IconButton>
        <IconButton aria-label="Settings" title="Settings" onClick={onSettings}>
          <Settings size={17} />
        </IconButton>
      </div>
    </header>
  );
}
