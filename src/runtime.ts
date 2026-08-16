import { produceArtifacts, type RefundWorkflowInput } from "./agents.js";
import { ControlPlane } from "./control-plane.js";
import type {
  AgentRole,
  ApprovalRecord,
  Artifact,
  EvidenceBundle,
  RuntimeEventType,
  TaskState
} from "./contracts.js";
import { InMemoryApprovalService } from "./ports.js";

export type RunResult = {
  task_id: string;
  state: TaskState;
  artifacts: Artifact[];
  evidence: EvidenceBundle;
  approval?: ApprovalRecord;
  rework_attempts: number;
  plane: ControlPlane;
  approvals: InMemoryApprovalService;
};

const HIGH_RISK_AMOUNT = 5000;

function emit(
  plane: ControlPlane,
  taskId: string,
  type: RuntimeEventType,
  producer: AgentRole | "control-plane"
): void {
  const { version } = plane.snapshot(taskId);
  const result = plane.accept({
    event_id: `${taskId}:${type}:${version}`,
    idempotency_key: `${taskId}:${type}:${version}`,
    trace_id: `${taskId}:trace`,
    task_id: taskId,
    state_version: version,
    type,
    producer,
    payload_summary: "redacted"
  });

  if (!result.accepted) {
    throw new Error(`event ${type} rejected`);
  }
}

function evidenceFor(
  plane: ControlPlane,
  taskId: string,
  artifacts: Artifact[],
  reviewDecision: "APPROVED" | "REWORK",
  approvalId?: string
): EvidenceBundle {
  const audit = plane.audit(taskId);
  return {
    evidence_id: `${taskId}:evidence`,
    task_id: taskId,
    event_ids: audit.map((event) => event.event_id),
    artifact_ids: artifacts.map((artifact) => artifact.artifact_id),
    review_decision: reviewDecision,
    approval_id: approvalId,
    audit_count: audit.length
  };
}

export function runRefundWorkflow(input: RefundWorkflowInput): RunResult {
  if (input.amount <= 0) {
    throw new Error("refund amount must be positive");
  }

  const plane = new ControlPlane();
  const approvals = new InMemoryApprovalService();
  const highRisk = input.amount > HIGH_RISK_AMOUNT;
  plane.register({
    task_id: input.task_id,
    tenant_id: "demo",
    objective: "Add refund request and supervisor approval workflow",
    risk_level: highRisk ? "HIGH" : "LOW"
  });

  emit(plane, input.task_id, "TaskCreated", "manager");
  emit(plane, input.task_id, "TaskPlanned", "manager");
  emit(plane, input.task_id, "TaskAssigned", "manager");
  emit(plane, input.task_id, "TaskStarted", "coding");

  const artifacts = produceArtifacts(input);
  for (const artifact of artifacts) {
    emit(plane, input.task_id, "ArtifactProduced", artifact.producer);
  }

  if (input.force_test_failure) {
    emit(plane, input.task_id, "ReworkRequested", "testing");
    emit(plane, input.task_id, "ReworkExhausted", "control-plane");
    return {
      task_id: input.task_id,
      state: "FAILED",
      artifacts,
      evidence: evidenceFor(plane, input.task_id, artifacts, "REWORK"),
      rework_attempts: 2,
      plane,
      approvals
    };
  }

  emit(plane, input.task_id, "ValidationCompleted", "testing");

  if (!highRisk) {
    emit(plane, input.task_id, "ReviewCompleted", "reviewer");
    emit(plane, input.task_id, "TaskCompleted", "control-plane");
    return {
      task_id: input.task_id,
      state: "COMPLETED",
      artifacts,
      evidence: evidenceFor(plane, input.task_id, artifacts, "APPROVED"),
      rework_attempts: 0,
      plane,
      approvals
    };
  }

  emit(plane, input.task_id, "ApprovalRequested", "control-plane");
  const approval = approvals.request({
    approval_id: `${input.task_id}:approval`,
    task_id: input.task_id,
    decision: "PENDING",
    risk_level: "HIGH",
    proposed_action: "Simulated refund approval workflow"
  });
  return {
    task_id: input.task_id,
    state: "WAITING_APPROVAL",
    artifacts,
    evidence: evidenceFor(plane, input.task_id, artifacts, "APPROVED", approval.approval_id),
    approval,
    rework_attempts: 0,
    plane,
    approvals
  };
}

export function decideRun(run: RunResult, actor: string, approved: boolean): RunResult {
  if (!run.approval || run.state !== "WAITING_APPROVAL") {
    throw new Error("task is not waiting for approval");
  }

  const approval = run.approvals.decide(run.approval.approval_id, actor, approved);
  if (!approved) {
    emit(run.plane, run.task_id, "ApprovalRejected", "control-plane");
    return {
      ...run,
      state: "FAILED",
      approval,
      evidence: evidenceFor(run.plane, run.task_id, run.artifacts, "REWORK", approval.approval_id)
    };
  }

  emit(run.plane, run.task_id, "ApprovalDecided", "control-plane");
  emit(run.plane, run.task_id, "TaskCompleted", "control-plane");
  return {
    ...run,
    state: "COMPLETED",
    approval,
    evidence: evidenceFor(run.plane, run.task_id, run.artifacts, "APPROVED", approval.approval_id)
  };
}
