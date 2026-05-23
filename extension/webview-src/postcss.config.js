/**
 * PostCSS pipeline for the ReCoder webview.
 *
 * Order matters:
 *   1. tailwindcss — expands @tailwind directives + utilities
 *   2. autoprefixer — adds vendor prefixes (mostly safe to skip in VSCode
 *      webview since Electron Chrome is recent, but kept for robustness)
 */
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
