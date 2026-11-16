// This plugin runs inside the DSH Web Host so the ownership check and cancel
// happen in the same event-loop turn as DSH's session and inbox operations.
import { realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, sep } from "node:path";

export const inject = ["connection", "agents", "workspaceRegistry", "workingDirectory"];

export function apply(ctx) {
  const workspace = realpathSync(process.cwd());
  function withinWorkspace(cwd) {
    try {
      const location = relative(workspace, realpathSync(cwd));
      return location && !isAbsolute(location) && location !== ".." && !location.startsWith(".." + sep)
        && statSync(cwd).isDirectory();
    } catch {
      return false;
    }
  }
  ctx.on("agent/pre-step", ({ agent }, next) => (
    withinWorkspace(agent.session.header.cwd) && withinWorkspace(ctx.workingDirectory.get(agent.session))
      ? next()
      : Promise.resolve({ kind: "reject" })
  ));
  const active = new WeakMap();
  ctx.on("session/event", (session, event) => {
    if (event.type === "turn/start") active.set(session, new Set());
    if (event.type === "user/message" && event.data.source?.kind === "user") {
      active.get(session)?.add(event.data.source.rpcId);
    }
    if (event.type === "turn/end") active.delete(session);
  });
  ctx.effect(() => ctx.connection.fetch.register({
    path: "/api/tentacle-bridge",
    methods: ["GET", "POST"],
    requestBody: "buffered",
    async fetch(request) {
      if (request.method === "GET") return Response.json({ version: 1 });
      const args = await request.json();
      if (typeof args.workspaceId === "string") {
        return Response.json({ path: ctx.workspaceRegistry.get(args.workspaceId)?.path ?? null });
      }
      const agent = ctx.agents.get(args.sessionId);
      const inputs = agent && active.get(agent.session);
      if (inputs?.size !== 1 || !inputs.has(args.requestId)) {
        return Response.json({ cancelled: false });
      }
      agent.cancel({ kind: "user" }, { keepInbox: true });
      return Response.json({ cancelled: true });
    },
  }), "tentacle: guarded session operations");
}
