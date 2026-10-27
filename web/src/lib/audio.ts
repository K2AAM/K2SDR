// Browser audio: AudioContext -> AudioWorklet (jitter buffer, resampling) -> gain (volume/mute).
// Volume and mute are browser-side only; the server stream stays pre-volume (decoder tap point).
// Browsers (iPhone Safari especially) only start or resume audio from a user gesture, and suspend
// it in the background: `needsTap` tells the UI to show a "tap to resume" control.
import workletUrl from "../player.worklet.ts?worker&url";
import type { JitterStats } from "./jitter";

export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private gain: GainNode | null = null;
  private volume = 0.8;
  private muted = false;
  stats: JitterStats | null = null;
  private listeners = new Set<() => void>();

  onChange(fn: () => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  private changed(): void { this.listeners.forEach((f) => f()); }

  get needsTap(): boolean { return this.ctx === null || this.ctx.state !== "running"; }
  get outputRate(): number | null { return this.ctx?.sampleRate ?? null; }

  /** Call from a user gesture (click/tap). Creates the audio graph once, then resumes it. */
  async startFromGesture(): Promise<void> {
    if (!this.ctx) {
      const ctx = new AudioContext({ latencyHint: "interactive" });
      this.ctx = ctx;
      ctx.onstatechange = () => this.changed();
      await ctx.audioWorklet.addModule(workletUrl);
      const node = new AudioWorkletNode(ctx, "k2-player", { numberOfInputs: 0, outputChannelCount: [1] });
      node.port.onmessage = (e: MessageEvent<JitterStats>) => { this.stats = e.data; this.changed(); };
      const gain = ctx.createGain();
      node.connect(gain).connect(ctx.destination);
      this.node = node;
      this.gain = gain;
      this.applyGain();
    }
    if (this.ctx.state !== "running") await this.ctx.resume();
    this.changed();
  }

  push(pcm: Int16Array): void {
    if (this.node && this.ctx?.state === "running") this.node.port.postMessage({ pcm: pcm.slice() });
  }

  flush(): void { this.node?.port.postMessage({ flush: true }); }

  setVolume(v: number): void { this.volume = Math.min(1, Math.max(0, v)); this.applyGain(); }
  setMuted(m: boolean): void { this.muted = m; this.applyGain(); }

  private applyGain(): void {
    if (this.gain && this.ctx) this.gain.gain.setTargetAtTime(this.muted ? 0 : this.volume ** 2, this.ctx.currentTime, 0.02);
  }
}
