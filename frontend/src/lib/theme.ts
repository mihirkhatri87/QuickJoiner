export type ThemeMode = "system" | "dark" | "light";

const KEY = "qj_theme";

export function getMode(): ThemeMode {
  return (localStorage.getItem(KEY) as ThemeMode) || "system";
}

export function applyTheme(mode: ThemeMode) {
  const root = document.documentElement;
  if (mode === "dark" || mode === "light") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
}

export function isDark(): boolean {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr) return attr === "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** Flip relative to what's on screen, persist, return the new effective darkness. */
export function toggleTheme(): boolean {
  const next: ThemeMode = isDark() ? "light" : "dark";
  localStorage.setItem(KEY, next);
  applyTheme(next);
  return next === "dark";
}
