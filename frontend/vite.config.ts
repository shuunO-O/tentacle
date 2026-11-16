import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 默认构建输出直达 web/static；支持通过 TENTACLE_OUT_DIR 覆盖构建目录。
const outDir = process.env.TENTACLE_OUT_DIR || "../web/static";
const apiTarget = process.env.TENTACLE_API_URL || "http://127.0.0.1:8421";

export default defineConfig({
  base: "/static/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  worker: {
    format: "es",
  },
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    manifest: false,
    assetsInlineLimit: 4096,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (id.includes("@xterm/")) return "vendor-xterm";
          if (id.includes("chart.js")) return "vendor-charts";
          if (
            id.includes("react-markdown") ||
            id.includes("remark-") ||
            id.includes("rehype-") ||
            id.includes("micromark") ||
            id.includes("mdast-") ||
            id.includes("hast-") ||
            id.includes("unist-") ||
            id.includes("unified") ||
            id.includes("vfile")
          ) {
            return "vendor-markdown";
          }
          if (
            id.includes("@radix-ui") ||
            id.includes("lucide-react") ||
            id.includes("cmdk") ||
            id.includes("react-resizable-panels")
          ) {
            return "vendor-ui";
          }
          if (id.includes("react-dom") || /node_modules[\\/](react|scheduler)[\\/]/.test(id)) {
            return "vendor-react";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/coding": { target: apiTarget, changeOrigin: true, ws: true },
      "/ws": { target: apiTarget.replace(/^http/, "ws"), ws: true },
    },
  },
});
