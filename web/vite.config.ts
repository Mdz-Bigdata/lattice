// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/zrender/")) return "chart-renderer";
          if (id.includes("/node_modules/echarts/")) return "charts";
          if (
            id.includes("/node_modules/react/") ||
            id.includes("/node_modules/react-dom/")
          )
            return "react";
        },
      },
    },
  },
});
