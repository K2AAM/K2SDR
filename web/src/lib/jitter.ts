// Jitter buffer + resampler for the 12 kS/s int16 listener audio (stream v1 AUDIO). Pure: the
// AudioWorklet (player.worklet.ts) wraps it; tests drive it directly.
//
// - Buffering: output silence until `targetMs` of audio is queued, then play. An underrun goes
//   back to buffering (and is counted), so playback resumes with a cushion instead of crackling.
// - Bounded latency: if more than `maxMs` is queued (the network delivered a burst after a stall),
//   the oldest audio is dropped down to the target.
// - flush(): a new channel generation or a discontinuity starts over (no audio of the old one).
// - Linear-interpolation resampling from 12 kS/s to the output rate.

export interface JitterStats { bufferedMs: number; underruns: number; dropsMs: number; playing: boolean }

export class JitterBuffer {
  private buf: Float32Array;
  private head = 0;           // read position (samples, mod capacity)
  private size = 0;           // queued samples
  private frac = 0;           // resampler phase between buf[head] and buf[head+1]
  private playing = false;
  underruns = 0;
  droppedSamples = 0;

  constructor(readonly inRate = 12000, readonly outRate = 48000, readonly targetMs = 250, readonly maxMs = 1000,
              capacityMs = 4000) {
    this.buf = new Float32Array(Math.ceil((inRate * capacityMs) / 1000));
  }

  private ms(n: number): number { return (n * 1000) / this.inRate; }

  push(pcm: Int16Array): void {
    const cap = this.buf.length;
    for (let i = 0; i < pcm.length; i++) {
      if (this.size === cap) { this.head = (this.head + 1) % cap; this.size--; this.droppedSamples++; }
      this.buf[(this.head + this.size) % cap] = pcm[i]! / 32768;
      this.size++;
    }
    const max = Math.floor((this.maxMs * this.inRate) / 1000);
    if (this.size > max) {
      const keep = Math.floor((this.targetMs * this.inRate) / 1000);
      const drop = this.size - keep;
      this.head = (this.head + drop) % cap;
      this.size = keep;
      this.droppedSamples += drop;
      this.frac = 0;
    }
  }

  flush(): void {
    this.size = 0;
    this.frac = 0;
    this.playing = false;
  }

  /** Fill `out` with samples at outRate. */
  render(out: Float32Array): void {
    const cap = this.buf.length;
    const step = this.inRate / this.outRate;
    if (!this.playing && this.ms(this.size) >= this.targetMs) this.playing = true;
    for (let i = 0; i < out.length; i++) {
      if (!this.playing || this.size < 2) {
        if (this.playing) { this.playing = false; this.underruns++; }
        out[i] = 0;
        continue;
      }
      const a = this.buf[this.head]!, b = this.buf[(this.head + 1) % cap]!;
      out[i] = a + (b - a) * this.frac;
      this.frac += step;
      while (this.frac >= 1 && this.size >= 2) {
        this.frac -= 1;
        this.head = (this.head + 1) % cap;
        this.size--;
      }
    }
  }

  stats(): JitterStats {
    return { bufferedMs: Math.round(this.ms(this.size)), underruns: this.underruns,
             dropsMs: Math.round(this.ms(this.droppedSamples)), playing: this.playing };
  }
}
