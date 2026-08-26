import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Build standalone -- o Dockerfile copia só `.next/standalone` +
  // `.next/static`, sem precisar de `node_modules` inteiro na imagem final.
  output: "standalone",
};

export default nextConfig;
