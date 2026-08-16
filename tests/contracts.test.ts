import { describe, expect, it } from "vitest";

import { RuntimeEventSchema, TaskEnvelopeSchema } from "../src/contracts.js";

describe("runtime contracts", () => {
  it("accepts a minimum task envelope", () => {
    const task = TaskEnvelopeSchema.parse({
      task_id: "task-1",
      tenant_id: "demo",
      objective: "Add refund approval",
      risk_level: "HIGH"
    });

    expect(task.task_id).toBe("task-1");
  });

  it("rejects an event without a trace identifier", () => {
    expect(() =>
      RuntimeEventSchema.parse({
        event_id: "event-1",
        idempotency_key: "key-1",
        task_id: "task-1",
        state_version: 0,
        type: "TaskCreated",
        producer: "manager",
        payload_summary: "redacted"
      })
    ).toThrow();
  });
});
