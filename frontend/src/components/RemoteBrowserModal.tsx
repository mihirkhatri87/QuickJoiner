import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { BrowserLoginJob, RemoteBrowserInputEvent } from "../types";
import { Button, cn } from "./ui";

/** Live view + remote control of a headless sign-in — the counterpart to a real window
 * opening on the QuickJoiner host, for when the host has no display at all (Docker/cloud).
 * The server streams the login page as polled JPEG frames (`GET .../browser/session/frames`);
 * clicks/keys sent from this canvas are applied to the real remote page
 * (`POST .../browser/session/input`). "Done — capture session" is the only way to end a
 * remote sign-in cleanly — there's no local window to close, so unlike the local flow this
 * needs an explicit signal from here.
 *
 * Like SyncLogModal, this is a viewer, not a leash: closing it (X / "Run in background")
 * only detaches — the sign-in keeps running server-side until it's captured or times out
 * from inactivity. */
export function RemoteBrowserModal({ name, onClose }: { name: string; onClose: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 1280, height: 800 });
  const [job, setJob] = useState<BrowserLoginJob | null>(null);
  const [streaming, setStreaming] = useState(true);
  const [doneRequested, setDoneRequested] = useState(false);
  const [err, setErr] = useState("");
  const attached = useRef(false);
  const lastMove = useRef(0);

  // Live frames — attach exactly once (StrictMode double-mount guard, same as SyncLogModal).
  useEffect(() => {
    if (attached.current) return;
    attached.current = true;
    let cancelled = false;
    (async () => {
      try {
        await api.streamBrowserFrames(name, (e) => {
          if (cancelled) return;
          if (e.type === "meta") setSize({ width: e.width, height: e.height });
          else if (e.type === "frame") void paintFrame(e.data);
          else if (e.type === "done") setStreaming(false);
        });
      } catch (streamErr) {
        if (!cancelled) setErr(String((streamErr as Error).message));
      }
      if (!cancelled) setStreaming(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  const paintFrame = async (b64: string) => {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
      const canvas = canvasRef.current;
      if (canvas) {
        if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
        }
        canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
      }
      bitmap.close();
    } catch {
      /* a dropped/corrupt frame just isn't drawn — the next one supersedes it, this is a
         live view, not a log with something worth retrying */
    }
  };

  // The frame stream carries no status text, so poll the job the same way SyncLogModal
  // polls sync jobs — cheap (verify=false), just enough to keep the message/state fresh.
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const s = await api.browserSession(name, false);
        if (!stop) setJob(s.login);
      } catch {
        /* transient — keep the last known job */
      }
    };
    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [name]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const interactive = Boolean(job?.active) && !doneRequested;

  // Input events MUST be delivered in order. Each one is its own HTTP POST, and concurrent
  // fetches have no ordering guarantee — fire-and-forget scrambled real typing (a keyup
  // overtaking its keydown, characters transposed, Backspace landing before the character it
  // was meant to delete). So they go through a strict FIFO with one request in flight.
  // Mousemove is *coalesced* rather than queued: it's the only high-frequency event, and a
  // stale cursor position is worthless — keeping them would starve the keystrokes behind them.
  const sendQueue = useRef<RemoteBrowserInputEvent[]>([]);
  const sending = useRef(false);
  const pump = useCallback(async () => {
    if (sending.current) return;
    sending.current = true;
    try {
      while (sendQueue.current.length) {
        const event = sendQueue.current.shift()!;
        try {
          await api.browserSessionInput(name, event);
        } catch {
          /* a lost event just doesn't land — keep the rest of the sequence moving */
        }
      }
    } finally {
      sending.current = false;
    }
  }, [name]);
  const sendInput = useCallback(
    (event: RemoteBrowserInputEvent) => {
      if (event.type === "mousemove") {
        const queued = sendQueue.current;
        const last = queued[queued.length - 1];
        if (last?.type === "mousemove") queued[queued.length - 1] = event;
        else queued.push(event);
      } else {
        sendQueue.current.push(event);
      }
      void pump();
    },
    [pump],
  );
  const buttonName = (b: number): "left" | "right" | "middle" =>
    b === 2 ? "right" : b === 1 ? "middle" : "left";
  const toCanvasCoords = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) * canvas.width) / rect.width,
      y: ((e.clientY - rect.top) * canvas.height) / rect.height,
    };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    const now = performance.now();
    if (now - lastMove.current < 30) return;
    lastMove.current = now;
    const { x, y } = toCanvasCoords(e);
    sendInput({ type: "mousemove", x, y });
  };
  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    e.currentTarget.focus();
    const { x, y } = toCanvasCoords(e);
    sendInput({ type: "mousedown", x, y, button: buttonName(e.button) });
  };
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    sendInput({ type: "mouseup", button: buttonName(e.button) });
  };
  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    e.preventDefault();
    sendInput({ type: "wheel", deltaX: e.deltaX, deltaY: e.deltaY });
  };
  // Keys fall into three buckets, and conflating them is what made typing unreliable:
  //  · modifier-only presses (Shift/Control/…) — dropped; `e.key` already carries the SHIFTED
  //    character, so replaying the modifier separately only risks a stuck modifier state.
  //  · a ctrl/cmd chord (Ctrl+A, Ctrl+V…) — sent as one `press` so the remote browser sees the
  //    combination rather than two unrelated key events.
  //  · everything else — a printable character goes as text insertion (exact, no keymap
  //    guesswork); a named key (Enter/Backspace/Delete/Tab/arrows) as keydown+keyup.
  const onKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    const key = e.key;
    // Ctrl/Cmd+V must be left ALONE — preventDefault() on this keydown cancels the browser's
    // paste action outright, so the paste event never fires and the clipboard never arrives
    // (measured; this is exactly why Ctrl+V silently did nothing). Bail before touching the
    // event, and let onPaste below deliver the text. Replaying the chord remotely would in any
    // case paste the *server's* clipboard, which is never what's wanted.
    if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === "v") return;
    e.preventDefault();
    if (key === "Shift" || key === "Control" || key === "Alt" || key === "Meta") return;
    if (e.ctrlKey || e.metaKey) {
      const mods = [e.ctrlKey || e.metaKey ? "Control" : "", e.altKey ? "Alt" : "",
                    e.shiftKey ? "Shift" : ""].filter(Boolean);
      sendInput({ type: "press", key: [...mods, key].join("+") });
      return;
    }
    if (key.length === 1) sendInput({ type: "type", text: key });
    else sendInput({ type: "keydown", key });
  };
  const onKeyUp = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    const key = e.key;
    if ((e.ctrlKey || e.metaKey) && key.toLowerCase() === "v") return;  // see onKeyDown
    e.preventDefault();
    // Only the named keys sent as keydown above need their matching release.
    if (key.length === 1 || e.ctrlKey || e.metaKey) return;
    if (key === "Shift" || key === "Control" || key === "Alt" || key === "Meta") return;
    sendInput({ type: "keyup", key });
  };
  // Paste has to be read from the LOCAL clipboard here and shipped as text — the remote
  // browser's clipboard is a different machine's and is always empty.
  const onPaste = (e: React.ClipboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return;
    e.preventDefault();
    const text = e.clipboardData.getData("text");
    if (text) sendInput({ type: "type", text });
  };

  const captureAndClose = async () => {
    setDoneRequested(true);
    try {
      await api.browserSessionDone(name);
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const finished = job !== null && !job.active;
  const dot = finished ? (job?.signed_in ? "bg-accent" : "bg-danger") : "bg-gold animate-pulse";

  // Portaled to <body>: this is rendered from deep inside SettingsDrawer, whose sliding
  // panel has `transition-transform` (even at translate-x-0, a `transform` is still applied) —
  // any CSS transform on an ancestor creates a new containing block for `position: fixed`
  // descendants, so without the portal this modal is positioned relative to the drawer panel
  // instead of the viewport (confined to its bounds instead of covering the screen).
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className="flex h-[80vh] w-[80vw] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <span className={cn("h-[8px] w-[8px] flex-shrink-0 rounded-full", dot)} />
          <div className="truncate text-[14px] font-semibold">Remote sign-in · {name}</div>
          <div className="ml-auto flex-shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-faint">
            {job?.state ?? "opening"}
          </div>
          <button
            onClick={onClose}
            className="flex-shrink-0 text-faint transition hover:text-ink"
            title="Close — the sign-in keeps running in the background"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 items-center justify-center bg-black/40 p-3">
          <canvas
            ref={canvasRef}
            width={size.width}
            height={size.height}
            tabIndex={0}
            onPointerMove={onPointerMove}
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            onWheel={onWheel}
            onKeyDown={onKeyDown}
            onKeyUp={onKeyUp}
            onPaste={onPaste}
            onContextMenu={(e) => e.preventDefault()}
            className={cn(
              "max-h-full max-w-full rounded-md bg-black focus:outline-none",
              interactive ? "cursor-default" : "cursor-not-allowed opacity-70",
            )}
            style={{ aspectRatio: `${size.width} / ${size.height}` }}
          />
        </div>

        <div className="border-t border-fill2 px-5 py-2.5 text-[11.5px] text-muted">
          {job?.message ||
            (streaming ? "Waiting for the remote browser to connect…" : "The remote view ended.")}
        </div>
        {err && <div className="px-5 pb-2 text-[11.5px] text-danger">{err}</div>}

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-fill2 px-5 py-3">
          <Button onClick={onClose}>{finished ? "Close" : "Run in background"}</Button>
          {!finished && (
            <Button variant="primary" onClick={captureAndClose} disabled={doneRequested}>
              {doneRequested ? "Capturing…" : "Done — capture session"}
            </Button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
