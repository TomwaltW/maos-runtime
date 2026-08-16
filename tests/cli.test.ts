import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, it } from "vitest";

import { writeDemoResult } from "../src/cli.js";

it("writes a completed and redacted demo result", () => {
  const path = join(mkdtempSync(join(tmpdir(), "maos-")), "run.json");

  writeDemoResult(path);

  const result = JSON.parse(readFileSync(path, "utf8"));
  expect(result.state).toBe("COMPLETED");
  expect(result.approval.decision).toBe("APPROVED");
  expect(JSON.stringify(result)).not.toContain("customer-secret");
});
