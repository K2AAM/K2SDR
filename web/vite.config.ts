import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

// Served by the control service under /app/. Strict CSP (script-src/style-src 'self'): no inline
// scripts or styles in the output, so component CSS is emitted as a file.
export default defineConfig({
  base: "/app/",
  plugins: [svelte({ emitCss: true })],
  build: { outDir: "dist", emptyOutDir: true, assetsInlineLimit: 0, sourcemap: false, target: "es2020" },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
