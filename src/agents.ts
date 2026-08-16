import { ArtifactSchema, type AgentRole, type Artifact } from "./contracts.js";

export type RefundWorkflowInput = {
  task_id: string;
  amount: number;
  force_test_failure: boolean;
};

type ArtifactDefinition = {
  kind: string;
  producer: AgentRole;
  summary: string;
};

export const agentIdentities = [
  { agent_id: "manager.v1", role: "manager", allowed_tools: ["plan.create"], forbidden_actions: ["complete-task", "external-write"] },
  { agent_id: "requirement.v1", role: "requirement", allowed_tools: ["requirements.normalize"], forbidden_actions: ["invent-business-rules"] },
  { agent_id: "architecture.v1", role: "architecture", allowed_tools: ["architecture.contracts"], forbidden_actions: ["modify-code"] },
  { agent_id: "coding.v1", role: "coding", allowed_tools: ["code.change.sandboxed"], forbidden_actions: ["production-write"] },
  { agent_id: "testing.v1", role: "testing", allowed_tools: ["test.verify"], forbidden_actions: ["complete-task"] },
  { agent_id: "reviewer.v1", role: "reviewer", allowed_tools: ["review.independent"], forbidden_actions: ["self-approve"] }
] as const;

export function produceArtifacts(input: RefundWorkflowInput): Artifact[] {
  const definitions: ArtifactDefinition[] = [
    { kind: "plan-dag", producer: "manager", summary: "Refund workflow plan with six constrained roles" },
    { kind: "requirements", producer: "requirement", summary: "Acceptance criteria, risks, and explicitly bounded assumptions" },
    { kind: "architecture-contract", producer: "architecture", summary: "Approval, audit, idempotency, and rollback contract" },
    { kind: "change-manifest", producer: "coding", summary: "Simulated isolated change with no external write" },
    { kind: "test-report", producer: "testing", summary: input.force_test_failure ? "FAILED: simulated acceptance check" : "PASSED: replayable acceptance checks" },
    { kind: "review-decision", producer: "reviewer", summary: input.force_test_failure ? "REWORK: test evidence blocks approval" : "APPROVED: independent review found complete evidence" }
  ];

  return definitions.map((definition) =>
    ArtifactSchema.parse({
      artifact_id: `${input.task_id}:${definition.kind}`,
      kind: definition.kind,
      producer: definition.producer,
      schema_version: "v1",
      content_hash: `sha256:simulated:${input.task_id}:${definition.kind}`,
      summary: definition.summary,
      dependencies: []
    })
  );
}
