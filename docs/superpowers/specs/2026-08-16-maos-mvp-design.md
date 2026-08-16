# MAOS Local Runtime MVP Design

## Purpose

Build the first public, locally runnable MAOS repository. It demonstrates a governed, deterministic multi-agent software-delivery loop for a refund-approval request without calling real models, production services, payment systems, or external tools.

The MVP implements the proposal's protocol-first, evidence-first, least-privilege principles before any MQ, gateway, database, or observability backend integration.

## Scope

### Included

- A TypeScript package that runs locally on Node.js.
- Versioned runtime contracts for tasks, events, artifacts, evidence, approvals, and agent identities, validated with Zod.
- An in-memory Control Plane as the exclusive owner of task state, event idempotency, audit records, and completion decisions.
- Six deterministic roles: Manager, Requirement, Architecture, Coding, Testing, and Reviewer.
- A direct-call orchestration flow for the refund-approval demonstration fixture.
- A Reviewer Gate that requires valid contracts, required artifacts, test evidence, and no blocking review defects before approval.
- High-risk action detection that pauses the task in `WAITING_APPROVAL` until an explicit simulated approval decision is supplied.
- Bounded rework with a maximum of two attempts.
- Fixtures and automated tests for a success path, test-failure rework, approval pause, and duplicate-event idempotency.
- A CLI example that writes a redacted, locally generated run record to `artifacts/run-result.json`.
- Public-project documentation: README, Apache-2.0 license, contributing guidance, and security policy.

### Excluded

- Calls to real LLMs, MCP servers, repositories, CI systems, payment systems, or production environments.
- Network-enabled code-change tools, real code patches, real database migrations, and real releases.
- RocketMQ, Nacos, Higress, PostgreSQL, OpenTelemetry, AgentTeams, web UI, multi-tenancy, and long-term memory implementations.
- Customer data, credentials, private prompts, unredacted logs, and production configuration.

## Architecture

The package is separated by responsibility. `contracts` defines and validates the data exchanged between roles. `control-plane` applies only permitted state transitions, deduplicates events using `idempotency_key`, records audit entries, and prevents any worker from directly completing a task. `agents` produces deterministic artifacts that emulate bounded specialist output. `runtime` builds the plan DAG, schedules roles in dependency order, invokes the gate, and creates the final evidence bundle.

Infrastructure seams are explicit interfaces. `EventBus`, `StateStore`, and `ApprovalService` have in-memory implementations for this release. A later release can add RocketMQ, PostgreSQL, or human-approval adapters without changing contracts or orchestration rules.

## Task Model

The demonstration task represents a refund-request and supervisor-approval workflow. A request that contains money movement, external writes, permission changes, or irreversible actions is high risk. The fixture deliberately uses a refund amount above the stated threshold so the runtime exercises the approval path while remaining a simulation.

The initial DAG is:

1. Manager classifies task risk and creates the plan.
2. Requirement normalizes the objective, acceptance criteria, risks, and open questions.
3. Architecture creates API, state, permission, idempotency, audit, and rollback contracts.
4. Coding creates a simulated authorized change artifact and manifest; it performs no filesystem mutation outside the generated run record.
5. Testing creates reproducible test evidence against the acceptance criteria.
6. Reviewer creates an independent decision and defect list.
7. Control Plane accepts the gate decision, pauses for approval if required, and then records completion or bounded rework.

## Lifecycle and Event Rules

The normal state path is:

`CREATED -> PLANNED -> ASSIGNED -> RUNNING -> VALIDATING -> REVIEW_PENDING -> APPROVED -> COMPLETED`

`WAITING_APPROVAL` is reachable from a pausable state when a high-risk action requires a human decision. An approved decision returns the task to the permitted transition path; a rejected or expired decision does not execute an external action and becomes `REWORK` or `FAILED` as specified by the fixture.

Events contain `event_id`, `idempotency_key`, `trace_id`, `task_id`, `state_version`, producer identity, timestamp, and a redacted payload summary. The Control Plane ignores a duplicate idempotency key and rejects an event whose version is stale. Workers may emit artifacts, evidence, errors, and heartbeats only; they cannot write final task state.

## Contracts and Evidence

Every artifact has a stable ID, schema version, producer, content hash, references to dependencies, and a summary. Large or sensitive values are represented by references and redacted summaries instead of copied payloads.

The Evidence Bundle contains the event sequence, required artifacts, test report, review decision, approval record where applicable, and audit index. The Gate blocks completion when required evidence is missing, a schema fails validation, a test fails, or Reviewer reports a blocking defect.

## Safety and Failure Handling

- Sensitive values are not stored in raw event payloads or CLI output.
- High-risk categories require a simulated approval record; no real action is ever invoked.
- The runtime accepts only an allow-listed event transition from each current state.
- Failed tests produce a minimal failure artifact and rework only the owning node.
- Rework is capped at two attempts; the next failure ends in a non-completed terminal outcome with evidence.
- Duplicate events do not repeat effects; stale events are rejected and audited.

## Testing Strategy

Tests run with Vitest and call the real in-memory runtime. They must prove:

1. A valid low-risk fixture reaches `COMPLETED` with an evidence bundle and audit trail.
2. A test failure produces a rework event, retries only the affected path, and records failure evidence.
3. A high-risk fixture pauses at `WAITING_APPROVAL`, cannot complete before a simulated approval, and completes only after approval.
4. A duplicate event with the same idempotency key does not create a second state change or audit side effect.
5. An invalid or stale event is rejected without advancing task state.

## Repository and Release Rules

The public repository is named `maos-runtime` and uses Apache-2.0. The README labels it an MVP simulator, documents local commands, maps features to the proposal, and states the non-production boundary. The initial remote is public, but no automated publishing or deployment is configured.

The initial release is considered ready only when the test suite passes, the CLI generates a redacted run result, source control contains no secrets or generated evidence, and the README's commands are reproducible from a fresh clone.

## Source

Derived from `MAOS_Multi-Agent_Collaboration_Runtime_Platform_Proposal.pdf`, especially its MVP scope, task lifecycle, context carriers, reusable skills, validation gates, risk boundaries, and open-source release layers.
