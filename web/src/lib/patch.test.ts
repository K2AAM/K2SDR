import { describe, expect, it } from "vitest";
import { applyOps, PatchError, type Json } from "./patch";

describe("applyOps (k2websdr.core.store.apply_ops semantics)", () => {
  it("adds, replaces and removes object members, with ~0/~1 escapes, copying values", () => {
    const doc: { [k: string]: Json } = { receivers: { "rx-0": { capture_gen: 1 } }, channels: {} };
    const value: Json = { mode: "usb" };
    applyOps(doc, [
      { op: "replace", path: "/receivers/rx-0/capture_gen", value: 2 },
      { op: "add", path: "/channels/ch_1", value },
      { op: "add", path: "/channels/a~1b~0c", value: 1 },
    ]);
    (value as { mode: string }).mode = "lsb";
    expect(doc).toEqual({ receivers: { "rx-0": { capture_gen: 2 } },
                          channels: { ch_1: { mode: "usb" }, "a/b~c": 1 } });
    applyOps(doc, [{ op: "remove", path: "/channels/ch_1" }]);
    expect(Object.keys(doc.channels as object)).toEqual(["a/b~c"]);
  });
  it("refuses replace/remove of missing members and missing parents", () => {
    expect(() => applyOps({}, [{ op: "replace", path: "/x", value: 1 }])).toThrow(PatchError);
    expect(() => applyOps({}, [{ op: "remove", path: "/x" }])).toThrow(PatchError);
    expect(() => applyOps({}, [{ op: "add", path: "/a/b", value: 1 }])).toThrow(PatchError);
  });
});
