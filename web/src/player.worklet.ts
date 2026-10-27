// AudioWorklet: plays the listener's 12 kS/s audio through the jitter buffer (lib/jitter.ts).
// Messages in: { pcm: Int16Array } | { flush: true }. Out, about once a second: JitterStats.
import { JitterBuffer } from "./lib/jitter";

declare const sampleRate: number;
declare function registerProcessor(name: string, ctor: unknown): void;
declare class AudioWorkletProcessor { readonly port: MessagePort; }

class Player extends AudioWorkletProcessor {
  private jb = new JitterBuffer(12000, sampleRate);
  private frames = 0;
  constructor() {
    super();
    this.port.onmessage = (e: MessageEvent<{ pcm?: Int16Array; flush?: boolean }>) => {
      if (e.data.flush) this.jb.flush();
      if (e.data.pcm) this.jb.push(e.data.pcm);
    };
  }
  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const out = outputs[0];
    if (out && out[0]) {
      this.jb.render(out[0]);
      for (let c = 1; c < out.length; c++) out[c]!.set(out[0]);
    }
    if ((this.frames += 128) >= sampleRate) {
      this.frames = 0;
      this.port.postMessage(this.jb.stats());
    }
    return true;
  }
}

registerProcessor("k2-player", Player);
