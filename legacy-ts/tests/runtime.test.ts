import { describe, expect, it } from "vitest";

import { decideRun, runRefundWorkflow } from "../src/runtime.js";

describe("refund workflow", () => {
  it("completes a low-risk run with all specialist artifacts and evidence", () => {
    const run = runRefundWorkflow({
      task_id: "low-risk",
      amount: 100,
      force_test_failure: false
    });

    expect(run.state).toBe("COMPLETED");
    expect(run.evidence.review_decision).toBe("APPROVED");
    expect(run.artifacts.map((artifact) => artifact.producer)).toEqual([
      "manager",
      "requirement",
      "architecture",
      "coding",
      "testing",
      "reviewer"
    ]);
  });

  it("records bounded rework and failure evidence when a test fails", () => {
    const run = runRefundWorkflow({
      task_id: "test-failure",
      amount: 100,
      force_test_failure: true
    });

    expect(run.state).toBe("FAILED");
    expect(run.rework_attempts).toBe(2);
    expect(run.plane.audit(run.task_id).map((event) => event.type)).toContain(
      "ReworkRequested"
    );
  });

  it("waits for a human approval before a high-risk task completes", () => {
    const pending = runRefundWorkflow({
      task_id: "high-risk",
      amount: 6000,
      force_test_failure: false
    });

    expect(pending.state).toBe("WAITING_APPROVAL");
    expect(decideRun(pending, "demo-approver", true).state).toBe("COMPLETED");
  });

  it("does not complete a high-risk task after an approval rejection", () => {
    const pending = runRefundWorkflow({
      task_id: "rejected-risk",
      amount: 6000,
      force_test_failure: false
    });

    const rejected = decideRun(pending, "demo-approver", false);

    expect(rejected.state).toBe("FAILED");
    expect(rejected.approval?.decision).toBe("REJECTED");
  });
});
