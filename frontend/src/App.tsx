import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api";
import type { CandidateItem, GapsResponse, ProjectRow, SessionRow, SourceRow, Status, SyncJob } from "./types";
import { buildCommands, startConnectFlow, type CommandCtx, type Flow } from "./commands";
import { ArtifactModal, type Artifact } from "./components/ArtifactModal";
import { Chat, type Msg } from "./components/Chat";
import { Composer } from "./components/Composer";
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
    { name: string; clean: boolean; autoStart: boolean; kind: "sync" | "cleanup" } | null
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
    (name: string, clean: boolean, autoStart = false, kind: "sync" | "cleanup" = "sync") => {
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
            ? { id: uid(), role: "user", text: m.content }
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
    if (!text || busy) return;
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

    if (!qj) {
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
    const agentId = uid();
    setMessages((m) => [
      ...m,
      { id: uid(), role: "user", text: effective, ts: now() },
      { id: agentId, role: "agent", streaming: true, ts: now() },
    ]);
    setBusy(true);
    const patch = (fn: (m: Msg) => Msg) =>
      setMessages((list) => list.map((x) => (x.id === agentId ? fn(x) : x)));
    try {
      await streamChat({ message: effective, session_id: sessionId, project: currentProject || null }, (e) => {
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
      await api.learn(artifact.markdown, artifact.title);
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
              <Composer value={input} onChange={setInput} onSend={send} disabled={busy} />
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
