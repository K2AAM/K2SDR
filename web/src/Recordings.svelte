<script lang="ts">
  // The signed-in user's recordings (admins: everyone's): download and delete over HTTP
  // (/api/recordings; delete needs the CSRF token). Active ones are stopped from the RIG.
  import { onMount } from "svelte";
  import { formatFrequency } from "./lib/freq";

  interface Row {
    id: string; owner: string; receiver: string; frequency_hz: number; mode: string; started_utc: string;
    ended_utc: string | null; status: string; detail: string | null; file: string; bytes: number; samples: number;
    gaps: number;
  }
  interface Props { csrf: string; onClose: () => void }
  let { csrf, onClose }: Props = $props();

  let rows: Row[] = $state([]);
  let error = $state("");

  async function load() {
    const r = await fetch("/api/recordings", { credentials: "same-origin" });
    if (!r.ok) { error = `cannot list recordings (${r.status})`; return; }
    rows = ((await r.json()) as { recordings: Row[] }).recordings;
  }
  async function remove(row: Row) {
    if (!confirm(`Delete ${row.file}?`)) return;
    const r = await fetch(`/api/recordings/${row.id}`, { method: "DELETE", credentials: "same-origin",
                                                        headers: { "X-CSRF-Token": csrf } });
    if (!r.ok) error = `delete failed (${r.status})`;
    await load();
  }
  onMount(() => { void load(); });

  const mb = (b: number) => `${(b / 1048576).toFixed(1)} MB`;
  const dur = (s: number) => `${Math.floor(s / 12000 / 60)}:${String(Math.floor(s / 12000) % 60).padStart(2, "0")}`;
</script>

<div class="drawer" role="dialog" aria-label="Recordings">
  <div class="top">
    <strong>Recordings</strong>
    <button onclick={load}>Refresh</button>
    <button onclick={onClose} aria-label="Close">✕</button>
  </div>
  {#if error}<p class="bad">{error}</p>{/if}
  {#if rows.length === 0}
    <p class="muted">No recordings yet.</p>
  {:else}
    <table>
      <thead><tr><th>UTC</th><th>Frequency</th><th>Mode</th><th>Length</th><th>Size</th><th>Status</th><th></th></tr></thead>
      <tbody>
        {#each rows as r (r.id)}
          <tr>
            <td>{r.started_utc.slice(0, 19).replace("T", " ")}</td>
            <td>{formatFrequency(r.frequency_hz)}</td>
            <td>{r.mode.toUpperCase()}</td>
            <td>{dur(r.samples)}</td>
            <td>{mb(r.bytes)}</td>
            <td title={r.detail ?? ""}>{r.status}{r.gaps ? ` (${r.gaps} gap${r.gaps === 1 ? "" : "s"})` : ""}</td>
            <td class="act">
              <a href="/api/recordings/{r.id}/file" download={r.file}>Download</a>
              {#if r.status !== "recording"}<button onclick={() => remove(r)}>Delete</button>{/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .drawer { position: fixed; inset: auto 0 0 0; max-height: 60vh; overflow: auto; background: var(--panel);
            border-top: 1px solid var(--line); padding: .5rem .75rem; z-index: 10; box-shadow: 0 -6px 18px rgba(0,0,0,.4); }
  .top { display: flex; gap: .5rem; align-items: center; margin-bottom: .4rem; }
  .top strong { margin-right: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: .25rem .4rem; border-bottom: 1px solid var(--line); white-space: nowrap; }
  td:nth-child(2) { font-variant-numeric: tabular-nums; }
  .act { display: flex; gap: .5rem; }
  a { color: var(--accent); }
  button { background: #0f1215; border: 1px solid var(--line); border-radius: 4px; padding: .1rem .45rem; cursor: pointer; }
  .muted { color: var(--muted); }
  .bad { color: var(--bad); }
</style>
