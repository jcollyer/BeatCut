import { defineConfig } from "vite";

// Tauri expects a fixed port and no clever clearing of the screen.
export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    // Don't watch the Rust side; cargo churns thousands of files in target/.
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    target: "es2021",
    outDir: "dist",
  },
});
