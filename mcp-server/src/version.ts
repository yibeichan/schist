// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import { createRequire } from "module";

// The version the server reports to clients in its `initialize` result, read
// from package.json so a release bump cannot miss it: a literal here stayed
// at 0.1.0 through 0.2.0. The path is relative to this module, which sits
// one level below package.json both as src/version.ts and as dist/version.js,
// and npm always ships package.json in the tarball.
const require = createRequire(import.meta.url);

export const SERVER_VERSION: string = (require("../package.json") as { version: string }).version;
