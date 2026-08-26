import { describe, expect, it } from "vitest";

import { ControlPlane } from "../src/control-plane.js";
import { InMemoryApprovalService } from "../src/ports.js";

const task = {
  task_id: "task-1",
  tenant_id: "demo",
  objective: "Add refund approval",
  risk_level: "LOW" as const
};

const event = (
  type:
    | "TaskCreated"
    | "TaskPlanned"
    | "TaskAssigned"
    | "TaskStarted"
    | "ValidationCompleted"
    | "ReviewCompleted"
    | "TaskCompleted",
  version: number,
  producer: "manager" | "coding" | "testing" | "reviewer" | "control-plane" = "manager",
  key = `${type}-${version}`
) => ({
  event_id: `${type}-${version}`,
  idempotency_key: key,
  trace_id: "trace-1",
  task_id: "task-1",
  state_version: version,
  type,
  producer,
  payload_summary: "redacted"
});

describe("ControlPlane", () => {
  it("ignores duplicate event keys without another audit record", () => {
    const plane = new ControlPlane();
    plane.register(task);

    expect(plane.accept(event("TaskCreated", 0))).toMatchObject({ accepted: true });
    expect(plane.accept(event("TaskCreated", 0))).toMatchObject({
      accepted: false,
      duplicate: true
    });
    expect(plane.audit(task.task_id)).toHaveLength(1);
  });

  it("rejects stale state versions", () => {
    const plane = new ControlPlane();
    plane.register(task);
    plane.accept(event("TaskCreated", 0));

    expect(plane.accept(event("TaskPlanned", 0))).toMatchObject({
      accepted: false,
      reason: "STALE_EVENT"
    });
  });

  it("does not let an agent complete a task even after review approval", () => {
    const plane = new ControlPlane();
    plane.register(task);
    plane.accept(event("TaskCreated", 0));
    plane.accept(event("TaskPlanned", 1));
    plane.accept(event("TaskAssigned", 2));
    plane.accept(event("TaskStarted", 3, "coding"));
    plane.accept(event("ValidationCompleted", 4, "testing"));
    plane.accept(event("ReviewCompleted", 5, "reviewer"));

    expect(plane.accept(event("TaskCompleted", 6))).toMatchObject({
      accepted: false,
      reason: "UNAUTHORIZED_PRODUCER"
    });
    expect(plane.snapshot(task.task_id).state).toBe("APPROVED");
  });
});

describe("InMemoryApprovalService", () => {
  it("records an explicit human approval decision", () => {
    const approvals = new InMemoryApprovalService();
    approvals.request({
      approval_id: "approval-1",
      task_id: "task-1",
      decision: "PENDING",
      risk_level: "HIGH",
      proposed_action: "simulated refund"
    });

    expect(approvals.decide("approval-1", "approver-1", true)).toMatchObject({
      decision: "APPROVED",
      actor: "approver-1"
    });
  });
});
