import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api";
import type { AskScope, CandidateItem, ChatAttachment, GapsResponse, ProjectRow, SessionRow, SourceRow, Status, SyncJob } from "./types";
import { buildCommands, startConnectFlow, type CommandCtx, type Flow } from "./commands";
import { ArtifactModal, type Artifact } from "./components/ArtifactModal";
import { Chat, type Msg } from "./components/Chat";
import { Composer } from "./components/Composer";
import { DocumentsModal } from "./components/DocumentsModal";
import { EMPTY_SCOPE, isScoped, ScopePicker } from "./components/ScopePicker";
import { EmptyState } from "./components/EmptyState";
import { GapsPanel } from "./components/GapsPanel";
import { GraphView } from "./components/GraphView";
import { Rail } from "./components/Rail";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { SyncHistoryModal } from "./components/SyncHistoryModal";
import { SyncLogModal } from "./components/SyncLogModal";
import { TopBar } from "./components/TopBar";

const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));
const now = () => new Date().toTimeString().slice(0, 5);

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [currentProject, setCurrentProject] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [memo, setMemo] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [view, setView] = useState<"chat" | "graph">("chat");
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [learnState, setLearnState] = useState<"idle" | "busy" | "done">("idle");
  const [gaps, setGaps] = useState<GapsResponse>({ open_count: 0, clusters: [] });
  const [gapsOpen, setGapsOpen] = useState(false);
  const [notifications, setNotifications] = useState<SyncJob[]>([]);
  // The sync log viewer lives here, not inside the settings drawer, so a running sync
  // stays reachable (from the bell menu or the rail) with the drawer closed.
  const [syncView, setSyncView] = useState<
    { name: string; clean: boolean; autoStart: boolean; kind: "sync" | "cleanup" | "regraph" } | null
  >(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const flowRef = useRef<Flow | null>(null); // active conversational flow (wizard, follow-up questions)
  const [rail, setRail] = useState(false); // mobile off-canvas overlay
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("qj_rail") === "collapsed");

  // One toggle serves both worlds: overlay below md, collapse at md+.
  const toggleRail = useCallback(() => {
    if (window.matchMedia("(min-width: 768px)").matches) {
      setCollapsed((v) => {
        localStorage.setItem("qj_rail", v ? "open" : "collapsed");
        return !v;
      });
    } else {
      setRail((v) => !v);
    }
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleRail();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleRail]);

  const loadStatus = useCallback(() => {
    api.status().then(setStatus).catch(() => setStatus(null));
  }, []);
  const loadSources = useCallback(() => {
    api.sources().then(setSources).catch(() => setSources([]));
  }, []);
  const loadProjects = useCallback(() => {
    api.projects().then(setProjects).catch(() => setProjects([]));
  }, []);
  const loadSessions = useCallback(
    (project = currentProject) => {
      api.sessions(project || undefined).then(setSessions).catch(() => setSessions([]));
    },
    [currentProject],
  );
  const loadGaps = useCallback(() => {
    api.gaps().then(setGaps).catch(() => setGaps({ open_count: 0, clusters: [] }));
  }, []);
  const loadNotifications = useCallback(() => {
    api.notifications().then((r) => setNotifications(r.notifications)).catch(() => {
      /* keep the last known feed rather than blanking it on a transient failure */
    });
  }, []);

  useEffect(() => {
    loadStatus();
    loadSources();
    loadProjects();
    loadSessions("");
    loadGaps();
    loadNotifications();
  }, [loadStatus, loadSources, loadProjects, loadSessions, loadGaps, loadNotifications]);

  const activeSyncs = notifications.filter(
    (n) => n.state === "running" || n.state === "stopping" || n.state === "retrying",
  );
  const activeCount = activeSyncs.length;

  // Poll the activity feed: briskly while something is syncing (so progress and the
  // finish land promptly), lazily when idle (so a scheduler- or CLI-started sync still
  // shows up without hammering the API). This is also what restores the picture after a
  // page reload — the jobs live on the server, so a refresh never loses them.
  useEffect(() => {
    const id = setInterval(loadNotifications, activeCount ? 3000 : 10000);
    return () => clearInterval(id);
  }, [loadNotifications, activeCount]);

  // When the last running sync finishes, the learned-memory counts and per-source doc
  // totals have changed — refresh them without making the user hunt for a reload.
  const prevActive = useRef(0);
  useEffect(() => {
    if (prevActive.current > 0 && activeCount === 0) {
      loadStatus();
      loadSources();
    }
    prevActive.current = activeCount;
  }, [activeCount, loadStatus, loadSources]);

  const openSync = useCallback(
    (name: string, clean: boolean, autoStart = false, kind: "sync" | "cleanup" | "regraph" = "sync") => {
      setSyncView({ name, clean, autoStart, kind });
    },
    [],
  );

  const newConversation = () => {
    setMessages([]);
    setMemo(null);
    setSessionId(null);
    setRail(false);
  };

  const openSession = async (id: string) => {
    try {
      const s = await api.session(id);
      setSessionId(s.id);
      setCurrentProject(s.project_id || "");
      setMemo(s.summary || null);
      const msgs: Msg[] = [];
      for (const m of s.messages) {
        if (m.role === "tool" || !m.content) continue;
        msgs.push(
          m.role === "user"
            ? { id: uid(), role: "user", text: m.content, attachments: m.attachments }
            : { id: uid(), role: "agent", answer: m.content },
        );
      }
      setMessages(msgs);
      setRail(false);
    } catch (e) {
      setMessages((m) => [...m, { id: uid(), role: "error", text: "Could not open conversation: " + e }]);
    }
  };

  const say = (text: string) => setMessages((m) => [...m, { id: uid(), role: "agent", text }]);
  const sayError = (text: string) => setMessages((m) => [...m, { id: uid(), role: "error", text }]);

  const deleteSession = async (id: string) => {
    try {
      await api.deleteSession(id);
      if (sessionId === id) newConversation();
      loadSessions();
    } catch (e) {
      sayError("Could not delete conversation: " + String(e));
    }
  };

  const deleteAllSessions = async () => {
    if (!window.confirm("Delete all conversations shown here? This can't be undone.")) return;
    try {
      await api.deleteAllSessions(currentProject || undefined);
      newConversation();
      loadSessions();
    } catch (e) {
      sayError("Could not delete conversations: " + String(e));
    }
  };
  const pushUser = (text: string) => setMessages((m) => [...m, { id: uid(), role: "user", text, ts: now() }]);

  // Per-question context files: the 📎 button / drag-drop STAGE files for the next question.
  // On send they upload as ephemeral context (NOT the Uploads connector / memory) and render
  // beneath the question. Kept for the conversation, auto-deleted after the retention window.
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  // Upload feedback for the staged files above — null while nothing is uploading. "uploading"
  // tracks real bytes sent; "processing" covers the gap between 100% sent and the response
  // coming back (server-side text extraction), so a multi-second wait after the bar fills
  // reads as "still working" instead of a frozen composer.
  const [uploadStatus, setUploadStatus] = useState<{ phase: "uploading" | "processing"; pct: number } | null>(null);
  // Opt-in: also ingest the staged attachments into permanent memory, not just use them
  // as context for this one question. Resets after each send so it can't silently persist.
  const [learnPending, setLearnPending] = useState(false);
  // Which slice of memory questions use. Sticky across a conversation (you usually ask
  // several things about the same source) and one click to clear. Empty = all of memory.
  const [scope, setScope] = useState<AskScope>(EMPTY_SCOPE);
  // The connector whose ingested documents are being browsed/labelled, if any.
  const [browsing, setBrowsing] = useState<SourceRow | null>(null);
  const attachFiles = (files: File[]) => setPendingFiles((prev) => [...prev, ...files]);
  const removePending = (idx: number) => setPendingFiles((prev) => prev.filter((_, i) => i !== idx));

  // Pipeline stages 1-2 (flows + command registry) live in commands.ts;
  // App provides the capabilities and keeps only the agentic-chat stage.
  const ctx: CommandCtx = {
    pushUser,
    say,
    sayError,
    addAgentPlaceholder: () => {
      const id = uid();
      setMessages((m) => [...m, { id, role: "agent", streaming: true, ts: now() }]);
      return id;
    },
    patchMessage: (id, fn) => setMessages((list) => list.map((x) => (x.id === id ? fn(x) : x))),
    setBusy,
    refresh: () => {
      loadSources();
      loadStatus();
      loadGaps();
    },
    setFlow: (f) => {
      flowRef.current = f;
    },
    openArtifact: (a) => {
      setArtifact(a);
      setLearnState("idle");
    },
  };
  const commands = buildCommands(ctx);

  const send = async () => {
    const text = input.trim();
    const hasAttachments = pendingFiles.length > 0;
    if ((!text && !hasAttachments) || busy) return;
    setInput("");

    // /qj <natural language>: control QuickJoiner in plain language. Strip the prefix and
    // send the rest straight to the agent (which has the qj_api control tools) — bypassing
    // the flow/command stages. This replaces the old /connect and /learn commands.
    const qj = text.match(/^\/qj\b\s*([\s\S]*)$/i);
    const effective = qj ? qj[1].trim() : text;
    if (qj && !effective) {
      ctx.say(
        "Type `/qj` then what you want — I control QuickJoiner through its own API. " +
          "e.g. `/qj connect a git repo for https://…`, `/qj teach: deploys happen on Fridays`, " +
          "`/qj sync the handbook connector`, or `/qj what can you change?`",
      );
      return;
    }

    // Slash commands / flows need typed text; an attach-only send (empty text + files) skips
    // straight to the agentic path so the files become the question's context.
    if (!qj && text) {
      // Stage 1: an active conversational flow gets first claim on the input.
      const flow = flowRef.current;
      if (flow && (await flow.handle(text))) return;

      // Stage 2: deterministic slash commands from the registry.
      for (const cmd of commands) {
        const m = text.match(cmd.match);
        if (m) {
          await cmd.run(m, text);
          return;
        }
      }
    }

    // Stage 3: the agentic path — /api/chat tool-call loop.
    const message = effective || "Please review the attached file(s) and tell me what they contain.";
    const userId = uid();
    const agentId = uid();
    // Push the question + a placeholder immediately — attaching/learning a large file can
    // take a while (see below), and it used to happen entirely before anything appeared in
    // the conversation, reading as a frozen composer with no way to tell it was still working.
    // The attachment chips and any learn-progress patch onto these same messages as work
    // actually happens, rather than waiting to render everything at once at the end.
    setMessages((m) => [
      ...m,
      { id: userId, role: "user", text: message, ts: now() },
      { id: agentId, role: "agent", streaming: true, ts: now() },
    ]);
    setBusy(true);
    const patch = (fn: (m: Msg) => Msg) =>
      setMessages((list) => list.map((x) => (x.id === agentId ? fn(x) : x)));
    const patchUser = (fn: (m: Msg) => Msg) =>
      setMessages((list) => list.map((x) => (x.id === userId ? fn(x) : x)));

    // Upload any staged context files first (ephemeral, NOT memory), so their ids ride the turn
    // and their metadata stamps the question. A failed upload aborts the send.
    let attachmentMeta: ChatAttachment[] = [];
    let attachmentIds: string[] = [];
    if (hasAttachments) {
      setUploadStatus({ phase: "uploading", pct: 0 });
      try {
        attachmentMeta = await api.uploadChatAttachments(pendingFiles, setUploadStatus);
        attachmentIds = attachmentMeta.map((a) => a.id);
        patchUser((m) => ({ ...m, attachments: attachmentMeta }));
      } catch (err) {
        setUploadStatus(null);
        setBusy(false);
        patch((m) => ({ ...m, role: "error", text: "Could not attach files: " + String(err), streaming: false }));
        return;
      } finally {
        setUploadStatus(null);
      }
      // Tell the user immediately when a file yielded no text, rather than letting them infer
      // it from a confused answer. This is the moment they can still do something about it.
      const unreadable = attachmentMeta.filter((a) => a.extract_error);
      if (unreadable.length) {
        sayError(
          unreadable.map((a) => `${a.filename}: ${a.extract_error}`).join("\n") +
            "\nI'll still answer from learned memory, but I can't read that file's contents.",
        );
      }
      // Opt-in "learn permanently": the same bytes are promoted into the Uploads connector,
      // so the file both answers THIS question and becomes cited memory. Deliberately
      // best-effort — a failed ingest must not lose the question the user just asked.
      // Embedding a large file (e.g. everything inside a zip) is real, possibly slow CPU
      // work — onProgress below gets REAL chunk-embedded/total counts (see
      // api.learnChatAttachment), not a fake number, so a multi-minute wait shows exactly
      // that instead of nothing.
      if (learnPending) {
        const label = `Learning ${attachmentMeta.map((a) => a.filename).join(", ")} permanently`;
        patch((m) => ({ ...m, learning: { label, done: 0, total: 0 } }));
        const failures: string[] = [];
        for (const att of attachmentMeta) {
          try {
            await api.learnChatAttachment(att.id, (p) =>
              patch((m) => (m.learning ? { ...m, learning: { ...m.learning, done: p.done, total: p.total } } : m)),
            );
          } catch (err) {
            failures.push(`${att.filename}: ${String((err as Error).message)}`);
          }
        }
        patch((m) => (m.learning ? { ...m, learning: { ...m.learning, complete: true } } : m));
        if (failures.length) sayError("Could not learn: " + failures.join("; "));
        loadStatus();
        loadSources();
      }
      setPendingFiles([]);
      setLearnPending(false);
    }
    try {
      await streamChat({
        message, session_id: sessionId, project: currentProject || null,
        attachment_ids: attachmentIds,
        // Only send a scope when there is one — an empty object would still mean "all
        // memory", but omitting it keeps the unscoped request byte-identical to before.
        scope: isScoped(scope) ? scope : undefined,
      }, (e) => {
        if (e.type === "thinking") patch((m) => ({ ...m, thinking: (m.thinking || "") + e.data }));
        else if (e.type === "tool_call") patch((m) => ({ ...m, tools: [...(m.tools || []), e.data] }));
        else if (e.type === "delta") patch((m) => ({ ...m, streamText: (m.streamText || "") + e.data }));
        else if (e.type === "candidates") {
          try {
            const items = JSON.parse(e.data) as CandidateItem[];
            patch((m) => ({ ...m, candidates: items }));
          } catch {
            /* malformed payload: ignore, the prose answer still renders */
          }
        } else if (e.type === "answer") {
          setSessionId(e.session_id);
          patch((m) => ({ ...m, answer: e.data, streaming: false, streamText: undefined }));
        } else if (e.type === "error")
          patch((m) => ({ ...m, role: "error", text: "Could not answer: " + e.data, streaming: false }));
      });
    } catch (err) {
      patch((m) => ({ ...m, role: "error", text: "Request failed: " + String(err), streaming: false }));
    } finally {
      setBusy(false);
      patch((m) => ({ ...m, streaming: false }));
      loadSessions();
      loadStatus();
    }
  };

  const distill = async () => {
    if (!sessionId) return;
    try {
      const r = await api.distill(sessionId);
      setMessages((m) => [
        ...m,
        { id: uid(), role: "agent", answer: `Saved to memory: ${r.facts_learned} durable fact(s) extracted.` },
      ]);
      loadSources();
      loadStatus();
    } catch (e) {
      setMessages((m) => [...m, { id: uid(), role: "error", text: "Could not save to memory: " + e }]);
    }
  };

  const learnArtifact = async () => {
    if (!artifact || learnState !== "idle") return;
    setLearnState("busy");
    try {
      // Shared: a crawl report describes an org system the user deliberately persisted, so
      // the commons is the intent here — unlike a personal note or an answer correction,
      // which stay private unless explicitly shared.
      await api.learn(artifact.markdown, artifact.title, true);
      setLearnState("done");
      say(`Learned **${artifact.title}** into memory — ask about it any time.`);
      loadSources();
      loadStatus();
    } catch (e) {
      setLearnState("idle");
      sayError("Could not learn the report: " + String(e));
    }
  };

  const learned = (status?.stats.documents ?? 0) > 0 || sources.some((s) => s.configured);

  return (
    <div className="flex h-full flex-col">
      <TopBar status={status} collapsed={collapsed} view={view} onMenu={toggleRail}
              onSettings={() => setDrawer(true)} onView={setView}
              notifications={notifications} onOpenSync={(name, clean) => openSync(name, clean)}
              onOpenHistory={() => setHistoryOpen(true)} />
      <div className="flex min-h-0 flex-1">
        <Rail
          open={rail}
          collapsed={collapsed}
          onClose={() => setRail(false)}
          status={status}
          projects={projects}
          currentProject={currentProject}
          onProject={(id) => {
            setCurrentProject(id);
            newConversation();
            loadSessions(id);
          }}
          onNewProject={async (name) => {
            try {
              const p = await api.createProject(name);
              setCurrentProject(p.id);
              await loadProjects();
              newConversation();
              loadSessions(p.id);
            } catch {
              /* ignore */
            }
          }}
          sessions={sessions}
          activeSession={sessionId}
          onOpenSession={openSession}
          onNewConversation={newConversation}
          onDeleteSession={deleteSession}
          onDeleteAll={deleteAllSessions}
          onDistill={distill}
          canDistill={!!sessionId}
          sources={sources}
          onManage={() => {
            setRail(false);
            setDrawer(true);
          }}
          gapCount={gaps.open_count}
          onOpenGaps={() => setGapsOpen(true)}
          syncJobs={notifications}
          onOpenSync={(name, clean) => openSync(name, clean)}
          onBrowseSource={(src) => {
            setRail(false);
            setBrowsing(src);
          }}
        />
        <section className="relative flex min-w-0 flex-1 flex-col">
          {view === "graph" ? (
            <GraphView />
          ) : (
            <>
              {messages.length === 0 ? (
                <div className="scroll-thin flex-1 overflow-y-auto px-5 py-8 md:px-10">
                  <EmptyState
                    learned={learned}
                    docs={status?.stats.documents ?? 0}
                    sources={status?.stats.sources ?? 0}
                    onConnect={() => setDrawer(true)}
                    onStarter={(q) => setInput(q)}
                  />
                </div>
              ) : (
                <Chat
                  messages={messages}
                  memo={memo}
                  onOpenArtifact={(a) => {
                    setArtifact(a);
                    setLearnState("idle");
                  }}
                  onLearned={() => {
                    loadStatus();
                    loadSources();
                    loadGaps();
                  }}
                />
              )}
              <Composer
                value={input}
                onChange={setInput}
                onSend={send}
                disabled={busy}
                pending={pendingFiles.map((f) => f.name)}
                onAttachFiles={attachFiles}
                onRemovePending={removePending}
                uploadStatus={uploadStatus}
                learnPending={learnPending}
                onToggleLearnPending={setLearnPending}
                scopeControl={
                  <ScopePicker scope={scope} onChange={setScope} sources={sources} />
                }
              />
            </>
          )}
        </section>
      </div>
      <SettingsDrawer
        open={drawer}
        onClose={() => setDrawer(false)}
        onChanged={() => {
          loadStatus();
          loadSources();
        }}
        onOpenArtifact={(a) => {
          setArtifact(a);
          setLearnState("done"); // repo brief is already ingested by the backend
        }}
        syncJobs={notifications}
        onOpenSync={openSync}
      />
      {browsing && (
        <DocumentsModal
          sourceId={browsing.id}
          sourceType={browsing.type}
          label={browsing.type === "uploads" ? "Uploaded documents" : browsing.name}
          onClose={() => setBrowsing(null)}
          onLabelsChanged={loadSources}
        />
      )}
      {historyOpen && (
        <SyncHistoryModal
          onClose={() => setHistoryOpen(false)}
          onOpenSync={(name, clean) => {
            setHistoryOpen(false);
            openSync(name, clean);
          }}
        />
      )}
      {syncView && (
        <SyncLogModal
          name={syncView.name}
          clean={syncView.clean}
          autoStart={syncView.autoStart}
          kind={syncView.kind}
          onClose={() => setSyncView(null)}
          onJobChange={() => {
            loadNotifications();
            loadSources();
            loadStatus();
          }}
        />
      )}
      <ArtifactModal
        artifact={artifact}
        learnState={learnState}
        onLearn={learnArtifact}
        onClose={() => setArtifact(null)}
      />
      <GapsPanel
        open={gapsOpen}
        data={gaps}
        onClose={() => setGapsOpen(false)}
        onConnect={(type) => {
          setGapsOpen(false);
          setView("chat");
          void startConnectFlow(ctx, type);
        }}
        onTeach={() => {
          setGapsOpen(false);
          setView("chat");
          setInput("/qj teach: ");
        }}
        onDismiss={async (cluster) => {
          try {
            await api.resolveGaps(cluster.gap_ids, "dismissed");
          } finally {
            loadGaps();
          }
        }}
      />
    </div>
  );
}
