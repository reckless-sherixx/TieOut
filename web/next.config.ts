import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * Emit `.next/standalone`: a self-contained server plus only the
   * node_modules it actually traced, so the runtime image copies ~50 MB
   * instead of the whole dependency tree.
   *
   * Harmless outside Docker -- `next dev` ignores it and `next build` writes
   * the directory in addition to what it already wrote, so the local flow is
   * unchanged.
   */
  output: "standalone",
};

export default nextConfig;
