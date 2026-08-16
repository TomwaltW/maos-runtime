import {
  RuntimeEventSchema,
  TaskEnvelopeSchema,
  type RuntimeEvent,
  type RuntimeEventType,
  type TaskEnvelope,
  type TaskEnvelopeInput,
  type TaskState
} from "./contracts.js";
import {
  InMemoryEventBus,
  InMemoryStateStore,
  type EventBus,
  type StateStore
} from "./ports.js";

export type TaskSnapshot = {
  envelope: TaskEnvelope;
  state: TaskState;
  version: number;
  idempotencyKeys: Set<string>;
  audit: RuntimeEvent[];
};

export type EventAcceptance =
  | { accepted: true; state: TaskState }
  | { accepted: false; duplicate: true }
  | { accepted: false; reason: "STALE_EVENT" | "INVALID_TRANSITION" | "UNAUTHORIZED_PRODUCER" };

const transitions: Record<RuntimeEventType, readonly [TaskState, TaskState] | undefined> = {
  TaskCreated: ["CREATED", "PLANNED"],
  TaskPlanned: ["PLANNED", "ASSIGNED"],
  TaskAssigned: ["ASSIGNED", "RUNNING"],
  TaskStarted: ["RUNNING", "VALIDATING"],
  ValidationCompleted: ["VALIDATING", "REVIEW_PENDING"],
  ReviewCompleted: ["REVIEW_PENDING", "APPROVED"],
  ApprovalRequested: ["REVIEW_PENDING", "WAITING_APPROVAL"],
  ApprovalDecided: ["WAITING_APPROVAL", "APPROVED"],
  ReworkRequested: ["VALIDATING", "REWORK"],
  ReworkExhausted: ["REWORK", "FAILED"],
  TaskCompleted: ["APPROVED", "COMPLETED"]
};

export class ControlPlane {
  private readonly store: StateStore<TaskSnapshot>;
  private readonly eventBus: EventBus;

  constructor(options: { store?: StateStore<TaskSnapshot>; eventBus?: EventBus } = {}) {
    this.store = options.store ?? new InMemoryStateStore<TaskSnapshot>();
    this.eventBus = options.eventBus ?? new InMemoryEventBus();
  }

  register(input: TaskEnvelopeInput): void {
    const envelope = TaskEnvelopeSchema.parse(input);
    this.store.set(envelope.task_id, {
      envelope,
      state: "CREATED",
      version: 0,
      idempotencyKeys: new Set(),
      audit: []
    });
  }

  accept(input: RuntimeEvent): EventAcceptance {
    const event = RuntimeEventSchema.parse(input);
    const task = this.requireTask(event.task_id);

    if (task.idempotencyKeys.has(event.idempotency_key)) {
      return { accepted: false, duplicate: true };
    }

    if (task.version !== event.state_version) {
      return { accepted: false, reason: "STALE_EVENT" };
    }

    if (event.type === "TaskCompleted" && event.producer !== "control-plane") {
      return { accepted: false, reason: "UNAUTHORIZED_PRODUCER" };
    }

    const transition = transitions[event.type];
    if (!transition || task.state !== transition[0]) {
      return { accepted: false, reason: "INVALID_TRANSITION" };
    }

    const next: TaskSnapshot = {
      ...task,
      state: transition[1],
      version: task.version + 1,
      idempotencyKeys: new Set([...task.idempotencyKeys, event.idempotency_key]),
      audit: [...task.audit, event]
    };

    this.store.set(event.task_id, next);
    this.eventBus.publish(event);
    return { accepted: true, state: next.state };
  }

  snapshot(taskId: string): Pick<TaskSnapshot, "state" | "version"> {
    const task = this.requireTask(taskId);
    return { state: task.state, version: task.version };
  }

  audit(taskId: string): RuntimeEvent[] {
    return [...this.requireTask(taskId).audit];
  }

  private requireTask(taskId: string): TaskSnapshot {
    const task = this.store.get(taskId);
    if (!task) {
      throw new Error("task not registered");
    }
    return task;
  }
}
