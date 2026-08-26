import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { decideRun, runRefundWorkflow } from "./runtime.js";

export function writeDemoResult(outputPath: string): void {
  const pending = runRefundWorkflow({
    task_id: "refund-demo",
    amount: 6000,
    force_test_failure: false
  });
  const result = decideRun(pending, "demo-approver", true);

  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(
    outputPath,
    `${JSON.stringify(
      {
        task_id: result.task_id,
        state: result.state,
        artifacts: result.artifacts,
        evidence: result.evidence,
        approval: result.approval
      },
      null,
      2
    )}\n`
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  writeDemoResult(resolve("artifacts/run-result.json"));
}
