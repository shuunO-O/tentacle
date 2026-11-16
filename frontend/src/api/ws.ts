import type { components } from "./schema";
import { getStoredToken } from "./client";

export type WsEventEnvelope = components["schemas"]["WsEventEnvelope"];
export type WsEventType = WsEventEnvelope["type"];
export type WsSubagentUpdatedData = components["schemas"]["WsSubagentUpdatedData"];
export type WsDedupProgressData = components["schemas"]["WsDedupProgressData"];
export type WsMaintenanceUpdatedData = components["schemas"]["WsMaintenanceUpdatedData"];

export type WsConnectionStatus = "idle" | "connecting" | "connected" | "reconnecting" | "unauthorized";

type EnvelopeListener = (event: WsEventEnvelope) => void;
type StatusListener = (status: WsConnectionStatus) => void;

class TentacleWsClient {
  private ws: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private attempt = 0;
  private stopped = true;
  private status: WsConnectionStatus = "idle";

  private anyListeners = new Set<EnvelopeListener>();
  private typedListeners = new Map<string, Set<EnvelopeListener>>();
  private statusListeners = new Set<StatusListener>();
  private unauthorizedCallback: (() => void) | null = null;

  setUnauthorizedCallback(fn: (() => void) | null): void {
    this.unauthorizedCallback = fn;
  }

  getStatus(): WsConnectionStatus {
    return this.status;
  }

  private setStatus(next: WsConnectionStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const listener of this.statusListeners) {
      listener(next);
    }
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  onAny(listener: EnvelopeListener): () => void {
    this.anyListeners.add(listener);
    return () => {
      this.anyListeners.delete(listener);
    };
  }

  on(type: WsEventType | string, listener: EnvelopeListener): () => void {
    let bucket = this.typedListeners.get(type);
    if (!bucket) {
      bucket = new Set();
      this.typedListeners.set(type, bucket);
    }
    bucket.add(listener);
    return () => {
      bucket?.delete(listener);
    };
  }

  connect(): void {
    this.stopped = false;
    this.clearReconnect();
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // Ignore close errors on stale sockets.
      }
      this.ws = null;
    }

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const token = getStoredToken();
    const url = `${proto}://${window.location.host}/ws/chat?token=${encodeURIComponent(token)}`;
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");

    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.attempt = 0;
      this.setStatus("connected");
    };

    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      try {
        const msg = JSON.parse(String(ev.data)) as WsEventEnvelope;
        for (const listener of this.anyListeners) {
          listener(msg);
        }
        const bucket = this.typedListeners.get(msg.type);
        if (bucket) {
          for (const listener of bucket) {
            listener(msg);
          }
        }
      } catch (err) {
        console.error("Invalid WS event payload", err);
      }
    };

    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (ev.code === 4401) {
        this.stopped = true;
        this.setStatus("unauthorized");
        this.unauthorizedCallback?.();
        return;
      }
      if (this.stopped) {
        this.setStatus("idle");
        return;
      }
      this.setStatus("reconnecting");
      this.scheduleReconnect();
    };
  }

  disconnect(): void {
    this.stopped = true;
    this.clearReconnect();
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // Ignore close errors during teardown.
      }
      this.ws = null;
    }
    this.setStatus("idle");
  }

  private scheduleReconnect(): void {
    this.clearReconnect();
    const delay = Math.min(1000 * Math.pow(1.6, this.attempt), 10_000);
    this.attempt += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) {
        this.connect();
      }
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

export const wsClient = new TentacleWsClient();
