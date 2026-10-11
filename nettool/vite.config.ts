import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  // Tauri expects a fixed port and reports Rust errors in the same terminal.
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: { target: "es2022" },
});
