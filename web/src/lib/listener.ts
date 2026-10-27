// The signed-in listener's channel on the selected receiver: opens it (and again after every
// reconnect, since closing a session closes its channels), tunes it, and routes its audio.
// Audio of an older channel_gen is never played (B6); a new generation flushes the jitter buffer.
// Settings are remembered per browser and receiver (outline §3: "remember ... per browser").
import type { Connection, Outcome } from "./client";
import type { Json } from "./patch";
import { Flags, type AudioMsg } from "./stream";

export type Agc = "off" | "fast" | "medium" | "slow";
export interface ListenerSettings {
  frequencyHz: number; mode: string; agc: Agc; passbandHz: [number, number] | null; manualGainDb: number;
  stepHz: number;
}
export interface ChannelView {
  id: string; frequency_hz: number; mode: string; channel_gen: number; agc: Agc; passband_hz: [number, number] | null;
  passband_default: boolean; manual_gain_db: number;
}

const KEY = (rx: string) => `k2.listener.${rx}`;

export function loadSettings(rx: string, fallbackHz: number): ListenerSettings {
  const d: ListenerSettings = { frequencyHz: fallbackHz, mode: "am", agc: "medium", passbandHz: null, manualGainDb: 30,
                                stepHz: 1000 };
  try {
    const s = JSON.parse(localStorage.getItem(KEY(rx)) ?? "null") as Partial<ListenerSettings> | null;
    if (!s) return d;
    return { ...d, ...s, frequencyHz: Number.isSafeInteger(s.frequencyHz) ? s.frequencyHz! : fallbackHz };
  } catch {
    return d;
  }
}

export function saveSettings(rx: string, s: ListenerSettings): void {
  try { localStorage.setItem(KEY(rx), JSON.stringify(s)); } catch { /* private mode: not remembered */ }
}

export class Listener {
  channelId: string | null = null;
  private gen = -1;
  private epoch = -1;                     // the connection epoch the channel was opened on
  private lastMeter = 0;
  levelDbfs: number | null = null;
  lastError: { code: string; detail: string } | null = null;
  private listeners = new Set<() => void>();
  private offs: (() => void)[] = [];

  constructor(private conn: Connection, readonly receiverId: string, public settings: ListenerSettings,
              private onAudio: (pcm: Int16Array, flush: boolean) => void) {
    // A new connection is a new server session, which closed our channel: open it again. The first
    // connect can fire after we already opened on it, so compare epochs instead of reopening blindly.
    this.offs.push(conn.onConnect(() => { if (conn.epoch !== this.epoch) { this.channelId = null; void this.open(); } }));
    this.offs.push(conn.onStream((m) => { if (m.kind === 2) this.audio(m); }));
    this.offs.push(conn.onState(() => this.changed()));
  }

  onChange(fn: () => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private changed(): void { this.listeners.forEach((f) => f()); }

  /** The channel as the server reports it (applied values). */
  get view(): ChannelView | null {
    const ch = this.channelId ? (conn_state(this.conn)?.channels as Record<string, Json> | undefined)?.[this.channelId] : undefined;
    return ch ? ({ id: this.channelId, ...(ch as object) } as unknown as ChannelView) : null;
  }

  private audio(m: AudioMsg): void {
    if (m.id !== this.channelId || m.gen < this.gen) return;      // not ours, or an older generation
    const fresh = m.gen > this.gen || (m.flags & Flags.NEW_GEN) !== 0;
    this.gen = m.gen;
    this.levelDbfs = m.levelCdb / 100;
    const now = performance.now();
    if (now - this.lastMeter > 200) { this.lastMeter = now; this.changed(); }   // S-meter: <= 5 updates/s
    this.onAudio(m.pcm, fresh || (m.flags & (Flags.DISCONTINUITY | Flags.SERVER_DROP)) !== 0);
  }

  private settle(o: Outcome): boolean {
    if (o.result?.status === "applied") {
      this.lastError = null;
      if (o.result.channel_gen != null && o.result.channel_gen > this.gen) this.gen = o.result.channel_gen;
      saveSettings(this.receiverId, this.settings);
      this.changed();
      return true;
    }
    const src = o.result ?? o.ack;
    this.lastError = { code: src.code ?? "E_UNKNOWN", detail: src.detail ?? "" };
    this.changed();
    return false;
  }

  async open(): Promise<boolean> {
    if (this.conn.status !== "open") return false;
    this.epoch = this.conn.epoch;
    const s = this.settings;
    const args: Record<string, Json> = { receiver: this.receiverId, frequency_hz: s.frequencyHz, mode: s.mode, agc: s.agc,
                                         manual_gain_db: s.manualGainDb };
    if (s.passbandHz) args.passband_hz = s.passbandHz;
    const o = await this.conn.command("channel.open", args);
    if (o.result?.status === "applied" && o.result.channel) {
      this.channelId = o.result.channel;
      this.gen = o.result.channel_gen ?? -1;
    }
    return this.settle(o);
  }

  /** Change any listener setting; opens the channel if there is none yet. */
  async tune(change: Partial<ListenerSettings> & { passbandDefault?: boolean }): Promise<boolean> {
    const prev = this.settings;
    const next: ListenerSettings = { ...prev, ...change };
    if (change.passbandDefault || (change.mode && change.mode !== prev.mode && !change.passbandHz)) next.passbandHz = null;
    if (!this.channelId) {
      this.settings = next;
      const ok = await this.open();
      if (!ok) this.settings = prev;
      return ok;
    }
    const args: Record<string, Json> = { channel: this.channelId };
    if (next.frequencyHz !== prev.frequencyHz) args.frequency_hz = next.frequencyHz;
    if (next.mode !== prev.mode) args.mode = next.mode;
    if (next.agc !== prev.agc) args.agc = next.agc;
    if (next.manualGainDb !== prev.manualGainDb) args.manual_gain_db = next.manualGainDb;
    if (change.passbandHz) args.passband_hz = change.passbandHz;
    else if (next.passbandHz === null && prev.passbandHz !== null && next.mode === prev.mode) args.passband_default = true;
    if (Object.keys(args).length === 1) {                         // only the step changed: local
      this.settings = next;
      saveSettings(this.receiverId, next);
      this.changed();
      return true;
    }
    const o = await this.conn.command("channel.tune", args);
    if (o.result?.status === "applied") this.settings = next;
    return this.settle(o);
  }

  close(): void {
    this.offs.forEach((f) => f());
    if (this.channelId) void this.conn.command("channel.close", { channel: this.channelId });
    this.channelId = null;
  }
}

function conn_state(c: Connection) { return c.state as { channels?: Json } | null; }
