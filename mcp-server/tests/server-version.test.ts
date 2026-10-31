// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The server reports its version to every client in `initialize`. It was a
 * literal in index.ts that no release bump touched, so 0.2.0 and the 0.3.0
 * build both announced 0.1.0. The version now comes from package.json; these
 * tests pin that and keep a literal from coming back.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SERVER_VERSION } from "../src/version.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(__dirname, "..");

describe("server version", () => {
  it("is package.json's version", () => {
    const pkg = JSON.parse(readFileSync(path.join(PKG, "package.json"), "utf-8")) as {
      version: string;
    };
    expect(SERVER_VERSION).toBe(pkg.version);
  });

  it("is what index.ts passes to the Server, with no version literal", () => {
    const src = readFileSync(path.join(PKG, "src", "index.ts"), "utf-8");
    const ctor = src.match(/new Server\(\s*\{([^}]*)\}/);
    expect(ctor).not.toBeNull();
    expect(ctor![1]).toMatch(/\bversion:\s*SERVER_VERSION\b/);
    expect(ctor![1]).not.toMatch(/version:\s*["'`]/);
  });
});
