// The control WebSocket (/ws): JSON control messages (control v1) and binary stream v1 frames on
// one socket. Keeps a state mirror from snapshot + patches, matches acks/results to commands, and
// reconnects with backoff. A 4401 close means the login session ended: go to /login.

import { applyOps, type Json, type PatchOp } from "./patch";
import { decode, type StreamMsg } from "./stream";
import { ulid } from "./ulid";

export type ConnStatus = "connecting" | "open" | "reconnecting" | "signed_out";

export interface Ack { type: "ack"; id: string; status: "accepted" | "rejected"; code?: string | null; detail?: string | null }
export interface Result {
  type: "result"; id: string; status: "applied" | "failed"; code?: string | null; detail?: string | null;
  capture_gen?: number | null; channel?: string | null; channel_gen?: number | null;
}
export interface Outcome { ack: Ack; result: Result | null }

interface Pending { resolve: (o: Outcome) => void; ack?: Ack }

export class Connection {
  status: ConnStatus = "connecting";
  state: { [k: string]: Json } | null = null;
  private rev = -1;
  private ws: WebSocket | null = null;
  private pending = new Map<string, Pending>();
  private retry = 0;
  private stateListeners = new Set<() => void>();
  private streamListeners = new Set<(m: StreamMsg) => void>();
  private statusListeners = new Set<(s: ConnStatus) => void>();
  private connectListeners = new Set<() => void>();

  constructor(private readonly url: string = Connection.defaultUrl()) {}

  static defaultUrl(): string {
    return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  }

  onState(fn: () => void): () => void { this.stateListeners.add(fn); return () => this.stateListeners.delete(fn); }
  onStream(fn: (m: StreamMsg) => void): () => void { this.streamListeners.add(fn); return () => this.streamListeners.delete(fn); }
  onStatus(fn: (s: ConnStatus) => void): () => void { this.statusListeners.add(fn); return () => this.statusListeners.delete(fn); }
  /** Called after every (re)connect, once the state is subscribed: re-establish session resources. */
  onConnect(fn: () => void): () => void { this.connectListeners.add(fn); return () => this.connectListeners.delete(fn); }

  connect(): void {
    const ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setStatus("open");
      void this.command("session.hello", { client: "k2websdr-web" });
      void this.subscribe().then(() => this.connectListeners.forEach((f) => f()));
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") this.onText(ev.data);
      else this.onBinary(ev.data as ArrayBuffer);
    };
    ws.onclose = (ev) => {
      this.ws = null;
      for (const [id, p] of this.pending) {
        p.resolve({ ack: p.ack ?? { type: "ack", id, status: "rejected", code: "E_DISCONNECTED", detail: "connection closed" },
                    result: null });
      }
      this.pending.clear();
      if (ev.code === 4401 || ev.code === 4403) {
        this.setStatus("signed_out");
        location.assign("/login");
        return;
      }
      this.setStatus("reconnecting");
      const delay = Math.min(10000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
  }

  private setStatus(s: ConnStatus): void {
    this.status = s;
    this.statusListeners.forEach((f) => f(s));
  }

  private async subscribe(): Promise<void> {
    this.rev = -1;
    await this.command("state.subscribe", {});
  }

  /** Send a command; resolves with its ack and (if accepted) its result. Never rejects. */
  command(op: string, args: Record<string, Json>): Promise<Outcome> {
    const id = ulid();
    return new Promise((resolve) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        resolve({ ack: { type: "ack", id, status: "rejected", code: "E_DISCONNECTED", detail: "not connected" }, result: null });
        return;
      }
      this.pending.set(id, { resolve });
      this.ws.send(JSON.stringify({ v: 1, type: "cmd", id, op, args }));
    });
  }

  private onText(text: string): void {
    const msg = JSON.parse(text) as { type: string; [k: string]: Json };
    switch (msg.type) {
      case "snapshot":
        this.state = msg.state as { [k: string]: Json };
        this.rev = msg.rev as number;
        this.stateListeners.forEach((f) => f());
        break;
      case "patch":
        if (this.state === null || msg.base !== this.rev) {   // missed one: start over
          void this.subscribe();
          return;
        }
        applyOps(this.state, msg.ops as unknown as PatchOp[]);
        this.rev = msg.rev as number;
        this.stateListeners.forEach((f) => f());
        break;
      case "ack": {
        const ack = msg as unknown as Ack;
        const p = this.pending.get(ack.id);
        if (!p) return;
        if (ack.status === "rejected") {
          this.pending.delete(ack.id);
          p.resolve({ ack, result: null });
        } else {
          p.ack = ack;
        }
        break;
      }
      case "result": {
        const res = msg as unknown as Result;
        const p = this.pending.get(res.id);
        if (!p) return;
        this.pending.delete(res.id);
        p.resolve({ ack: p.ack ?? { type: "ack", id: res.id, status: "accepted" }, result: res });
        break;
      }
      default:
        break;                                                 // "error": logged by the server side
    }
  }

  private onBinary(buf: ArrayBuffer): void {
    let m: StreamMsg;
    try {
      m = decode(buf);
    } catch {
      return;                                                  // a malformed frame is dropped, never shown
    }
    this.streamListeners.forEach((f) => f(m));
  }
}
