/* Conversational /connect wizard: a small state machine over the connector
 * catalog (/api/connectors/types). Pure functions — App.tsx feeds it user
 * input and interprets the returned messages/effects, so all API calls stay
 * in one place. 'cancel' aborts at any step. */

import type { ConnectorField, ConnectorType } from "./types";

export interface WizardState {
  step: "type" | "name" | "field" | "shared" | "failsave" | "sync";
  types: ConnectorType[];
  type?: ConnectorType;
  name?: string;
  idx: number;
  values: Record<string, unknown>;
  shared?: boolean;
}

export interface WizardResult {
  state: WizardState | null; // null = wizard over
  say?: string; // agent message to show
  effect?: { kind: "create"; skipTest: boolean } | { kind: "sync" };
}

const CANCEL = /^cancel$/i;
const YES = /^(y|yes|yep|sure|ok(ay)?)$/i;

export function startWizard(
  types: ConnectorType[],
  preselectType?: string,
): { state: WizardState; say: string } {
  // A valid preselect (e.g. from a gap's "Connect octopus" CTA) skips the type step
  // and jumps straight to naming the connector.
  const pre = preselectType
    ? types.find(
        (t) => t.type === preselectType || t.label.toLowerCase() === preselectType.toLowerCase(),
      )
    : undefined;
  if (pre) {
    return {
      state: { step: "name", types, type: pre, idx: 0, values: {} },
      say: `Connecting **${pre.label}**. What should this connector be called? (short name, e.g. \`team-wiki\`)`,
    };
  }
  const list = types.map((t) => `\`${t.type}\` — ${t.label}`).join("\n");
  return {
    state: { step: "type", types, idx: 0, values: {} },
    say:
      "Let's set up a connector. Which type do you want to connect?\n" +
      `${list}\n\nReply with a type (or 'cancel' to stop).`,
  };
}

function fieldPrompt(f: ConnectorField): string {
  const bits = [f.required ? "required" : "optional — reply 'skip' to leave empty"];
  if (f.secret) bits.push(`secret — prefer env indirection like env:${f.env || "MY_TOKEN"}`);
  if (f.list) bits.push("accepts a comma-separated list");
  const hint = [f.help, f.placeholder && `e.g. ${f.placeholder}`].filter(Boolean).join(" ");
  return `**${f.label}** (${bits.join("; ")})${hint ? `\n${hint}` : ""}`;
}

function nextFieldOrShared(state: WizardState): WizardResult {
  const fields = state.type!.fields;
  if (state.idx < fields.length) {
    return { state, say: fieldPrompt(fields[state.idx]) };
  }
  state.step = "shared";
  return {
    state,
    say: "Share this connector (and its live tools) with all users? (yes/no — 'no' keeps it private to you; in open mode everything is shared anyway)",
  };
}

export function wizardInput(state: WizardState, raw: string): WizardResult {
  const input = raw.trim();
  if (CANCEL.test(input)) return { state: null, say: "Connector setup cancelled." };

  if (state.step === "type") {
    const wanted = input.toLowerCase();
    const type = state.types.find(
      (t) => t.type === wanted || t.label.toLowerCase() === wanted,
    );
    if (!type) {
      return {
        state,
        say: `I don't know the type \`${input}\`. Available: ${state.types.map((t) => t.type).join(", ")} — or 'cancel'.`,
      };
    }
    state.type = type;
    state.step = "name";
    return { state, say: `Connecting **${type.label}**. What should this connector be called? (short name, e.g. \`team-wiki\`)` };
  }

  if (state.step === "name") {
    if (!/^[\w.-]{2,64}$/.test(input)) {
      return { state, say: "Names are 2-64 letters/digits/dots/dashes — try again (or 'cancel')." };
    }
    state.name = input;
    state.step = "field";
    state.idx = 0;
    return nextFieldOrShared(state);
  }

  if (state.step === "field") {
    const field = state.type!.fields[state.idx];
    const skipped = /^(skip|-)$/i.test(input) || input === "";
    if (skipped && field.required) {
      return { state, say: `**${field.label}** is required for ${state.type!.type}. ` + fieldPrompt(field) };
    }
    if (!skipped) {
      state.values[field.key] = field.list
        ? input.split(",").map((s) => s.trim()).filter(Boolean)
        : input;
    }
    state.idx += 1;
    return nextFieldOrShared(state);
  }

  if (state.step === "shared") {
    state.shared = YES.test(input);
    return {
      state,
      say: "Testing the connection and saving…",
      effect: { kind: "create", skipTest: false },
    };
  }

  if (state.step === "failsave") {
    if (/^save$/i.test(input)) {
      return { state, say: "Saving without a test…", effect: { kind: "create", skipTest: true } };
    }
    return { state: null, say: "Connector setup cancelled." };
  }

  if (state.step === "sync") {
    if (YES.test(input)) return { state, effect: { kind: "sync" } };
    return { state: null, say: "Done — you can sync any time from Settings or with `qj sync`." };
  }

  return { state: null };
}
