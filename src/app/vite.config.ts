import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import path from "path";

export default defineConfig({
  root: "src/bhe_catalog/ui",
  plugins: [
    TanStackRouterVite({
      routesDirectory: path.resolve(__dirname, "src/bhe_catalog/ui/routes"),
      generatedRouteTree: path.resolve(
        __dirname,
        "src/bhe_catalog/ui/types/routeTree.gen.ts",
      ),
    }),
    react(),
    tailwindcss(),
  ],
  define: {
    __APP_NAME__: JSON.stringify("BHE Data Catalog"),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src/bhe_catalog/ui"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, "src/bhe_catalog/__dist__"),
    emptyOutDir: true,
  },
});
