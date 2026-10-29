import { describe, expect, it } from "vitest";
import type { Connection, Outcome } from "./client";
import { Listener, type ListenerSettings } from "./listener";

// A fake server: channel.open makes "ch<n>"; channel.tune on an unknown id is rejected like the
// real service ("no channel N"). The test removes channels from the state as a retune would.
class FakeConn {
  status = "open"; epoch = 1; state: { channels: Record<string, unknown> } = { channels: {} };
  ops: string[] = []; private n = 0; private stateFns: (() => void)[] = [];
  onConnect() { return () => {}; }
  onStream() { return () => {}; }
  onState(fn: () => void) { this.stateFns.push(fn); return () => {}; }
  emit() { this.stateFns.forEach((f) => f()); }
  async command(op: string, args: Record<string, unknown>): Promise<Outcome> {
    this.ops.push(`${op} ${args.channel ?? ""} ${args.frequency_hz ?? ""}`.trim());
    if (op === "channel.open") {
      const id = `ch${++this.n}`;
      this.state.channels[id] = { frequency_hz: args.frequency_hz };
      return { ack: { type: "ack", id: "x", status: "accepted" },
               result: { type: "result", id: "x", status: "applied", channel: id, channel_gen: 1 } } as Outcome;
    }
    if (!(String(args.channel) in this.state.channels))
      return { ack: { type: "ack", id: "x", status: "rejected", code: "E_UNKNOWN_CHANNEL", detail: `no channel ${args.channel}` },
               result: null } as Outcome;
    return { ack: { type: "ack", id: "x", status: "accepted" },
             result: { type: "result", id: "x", status: "applied", channel_gen: 2 } } as Outcome;
  }
}

const settings = (): ListenerSettings =>
  ({ frequencyHz: 7_074_000, mode: "usb", agc: "medium", passbandHz: null, manualGainDb: 30, stepHz: 1000 });

describe("Listener after the server closes its channel", () => {
  it("a tune that finds the channel gone opens a new one with the change", async () => {
    const c = new FakeConn();
    const l = new Listener(c as unknown as Connection, "rx", settings(), () => {});
    await l.open();
    delete c.state.channels.ch1;                 // receiver retuned away; no state patch seen yet
    expect(await l.tune({ frequencyHz: 10_000_000 })).toBe(true);
    expect(l.channelId).toBe("ch2");
    expect(l.lastError).toBeNull();
    expect(c.ops[c.ops.length - 1]).toBe("channel.open  10000000");
  });

  it("a removal seen in the state reopens once, and a tune waits for it", async () => {
    const c = new FakeConn();
    const l = new Listener(c as unknown as Connection, "rx", settings(), () => {});
    await l.open();
    c.emit();                                    // the channel is in the state
    delete c.state.channels.ch1;
    c.emit(); c.emit();                          // removed (two patches: still one reopen)
    expect(await l.tune({ mode: "lsb" })).toBe(true);
    expect(c.ops.filter((o) => o.startsWith("channel.open")).length).toBe(2);
    expect(c.ops[c.ops.length - 1]).toBe("channel.tune ch2");
  });

  it("a channel not yet in the state is not taken for lost", async () => {
    const c = new FakeConn();
    const l = new Listener(c as unknown as Connection, "rx", settings(), () => {});
    c.state.channels = {};
    await l.open();
    delete c.state.channels.ch1;
    c.emit();
    expect(l.channelId).toBe("ch1");
  });
});

describe("Listener.nudge", () => {
  it("coalesces a burst into few tunes that end on the newest target, snapped to the step", async () => {
    const c = new FakeConn();
    const l = new Listener(c as unknown as Connection, "rx", settings(), () => {});
    await l.open();
    const burst = [l.nudge(1, 1000), l.nudge(1, 1000), l.nudge(1, 1000), l.nudge(1, 1000)];
    await Promise.all(burst);
    const tunes = c.ops.filter((o) => o.startsWith("channel.tune"));
    expect(tunes.length).toBeLessThanOrEqual(2);
    expect(l.settings.frequencyHz).toBe(7_078_000);
    await l.nudge(-1, 500);
    expect(l.settings.frequencyHz).toBe(7_077_500);
  });
});
