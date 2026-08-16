import type { ApprovalRecord, RuntimeEvent } from "./contracts.js";

export interface EventBus {
  publish(event: RuntimeEvent): void;
  events(taskId: string): RuntimeEvent[];
}

export interface StateStore<T> {
  get(id: string): T | undefined;
  set(id: string, value: T): void;
}

export interface ApprovalService {
  request(record: ApprovalRecord): ApprovalRecord;
  decide(id: string, actor: string, approved: boolean): ApprovalRecord;
  get(id: string): ApprovalRecord | undefined;
}

export class InMemoryEventBus implements EventBus {
  private readonly values: RuntimeEvent[] = [];

  publish(event: RuntimeEvent): void {
    this.values.push(event);
  }

  events(taskId: string): RuntimeEvent[] {
    return this.values.filter((event) => event.task_id === taskId);
  }
}

export class InMemoryStateStore<T> implements StateStore<T> {
  private readonly values = new Map<string, T>();

  get(id: string): T | undefined {
    return this.values.get(id);
  }

  set(id: string, value: T): void {
    this.values.set(id, value);
  }
}

export class InMemoryApprovalService implements ApprovalService {
  private readonly values = new Map<string, ApprovalRecord>();

  request(record: ApprovalRecord): ApprovalRecord {
    this.values.set(record.approval_id, record);
    return record;
  }

  decide(id: string, actor: string, approved: boolean): ApprovalRecord {
    const record = this.values.get(id);

    if (!record) {
      throw new Error("approval not found");
    }

    const next: ApprovalRecord = {
      ...record,
      actor,
      decision: approved ? "APPROVED" : "REJECTED"
    };
    this.values.set(id, next);
    return next;
  }

  get(id: string): ApprovalRecord | undefined {
    return this.values.get(id);
  }
}
