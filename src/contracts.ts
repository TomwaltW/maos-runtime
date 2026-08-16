import { z } from "zod";

export const RiskLevelSchema = z.enum(["LOW", "MEDIUM", "HIGH", "CRITICAL"]);
export type RiskLevel = z.infer<typeof RiskLevelSchema>;

export const TaskStateSchema = z.enum([
  "CREATED",
  "PLANNED",
  "ASSIGNED",
  "RUNNING",
  "VALIDATING",
  "REVIEW_PENDING",
  "WAITING_APPROVAL",
  "APPROVED",
  "REWORK",
  "FAILED",
  "COMPLETED"
]);
export type TaskState = z.infer<typeof TaskStateSchema>;

export const AgentRoleSchema = z.enum([
  "manager",
  "requirement",
  "architecture",
  "coding",
  "testing",
  "reviewer"
]);
export type AgentRole = z.infer<typeof AgentRoleSchema>;

export const AgentIdentitySchema = z.object({
  agent_id: z.string().min(1),
  role: AgentRoleSchema,
  allowed_tools: z.array(z.string().min(1)),
  forbidden_actions: z.array(z.string().min(1))
});
export type AgentIdentity = z.infer<typeof AgentIdentitySchema>;

export const TaskEnvelopeSchema = z.object({
  task_id: z.string().min(1),
  tenant_id: z.string().min(1),
  objective: z.string().min(1),
  risk_level: RiskLevelSchema,
  priority: z.number().int().min(1).max(5).default(3),
  parent_task_id: z.string().min(1).optional()
});
export type TaskEnvelope = z.infer<typeof TaskEnvelopeSchema>;

export const RuntimeEventTypeSchema = z.enum([
  "TaskCreated",
  "TaskPlanned",
  "TaskAssigned",
  "TaskStarted",
  "ValidationCompleted",
  "ReviewCompleted",
  "ApprovalRequested",
  "ApprovalDecided",
  "ReworkRequested",
  "ReworkExhausted",
  "TaskCompleted"
]);
export type RuntimeEventType = z.infer<typeof RuntimeEventTypeSchema>;

export const RuntimeEventSchema = z.object({
  event_id: z.string().min(1),
  idempotency_key: z.string().min(1),
  trace_id: z.string().min(1),
  task_id: z.string().min(1),
  state_version: z.number().int().nonnegative(),
  type: RuntimeEventTypeSchema,
  producer: AgentRoleSchema.or(z.literal("control-plane")),
  payload_summary: z.string().min(1),
  occurred_at: z.string().datetime().optional()
});
export type RuntimeEvent = z.infer<typeof RuntimeEventSchema>;

export const ArtifactSchema = z.object({
  artifact_id: z.string().min(1),
  kind: z.string().min(1),
  producer: AgentRoleSchema,
  schema_version: z.literal("v1"),
  content_hash: z.string().min(1),
  summary: z.string().min(1),
  dependencies: z.array(z.string().min(1))
});
export type Artifact = z.infer<typeof ArtifactSchema>;

export const ApprovalRecordSchema = z.object({
  approval_id: z.string().min(1),
  task_id: z.string().min(1),
  decision: z.enum(["PENDING", "APPROVED", "REJECTED"]),
  risk_level: z.enum(["HIGH", "CRITICAL"]),
  proposed_action: z.string().min(1),
  actor: z.string().min(1).optional()
});
export type ApprovalRecord = z.infer<typeof ApprovalRecordSchema>;

export const EvidenceBundleSchema = z.object({
  evidence_id: z.string().min(1),
  task_id: z.string().min(1),
  event_ids: z.array(z.string().min(1)),
  artifact_ids: z.array(z.string().min(1)),
  review_decision: z.enum(["APPROVED", "REWORK"]),
  approval_id: z.string().min(1).optional(),
  audit_count: z.number().int().nonnegative()
});
export type EvidenceBundle = z.infer<typeof EvidenceBundleSchema>;
