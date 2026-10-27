// The server's state mirror protocol: one snapshot, then patches (RFC 6902 add/replace/remove on
// object members, k2websdr.core.store.apply_ops). A patch whose base is not our rev means we
// missed one: the caller resubscribes.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export interface PatchOp { op: "add" | "replace" | "remove"; path: string; value?: Json }

export class PatchError extends Error {}

function parsePointer(path: string): string[] {
  if (!path.startsWith("/")) throw new PatchError(`bad pointer ${path}`);
  return path.slice(1).split("/").map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"));
}

function clone<T extends Json>(v: T): T {
  return v === null || typeof v !== "object" ? v : (JSON.parse(JSON.stringify(v)) as T);
}

export function applyOps(doc: { [k: string]: Json }, ops: PatchOp[]): void {
  for (const op of ops) {
    const parts = parsePointer(op.path);
    const last = parts.pop()!;
    let node: Json = doc;
    for (const p of parts) {
      if (node === null || typeof node !== "object" || Array.isArray(node) || !(p in node)) {
        throw new PatchError(`missing parent for ${op.path}`);
      }
      node = (node as { [k: string]: Json })[p]!;
    }
    if (node === null || typeof node !== "object" || Array.isArray(node)) throw new PatchError(`bad parent ${op.path}`);
    const obj = node as { [k: string]: Json };
    if (op.op === "remove") {
      if (!(last in obj)) throw new PatchError(`remove of missing ${op.path}`);
      delete obj[last];
    } else if (op.op === "replace") {
      if (!(last in obj)) throw new PatchError(`replace of missing ${op.path}`);
      obj[last] = clone(op.value ?? null);
    } else {
      obj[last] = clone(op.value ?? null);
    }
  }
}
