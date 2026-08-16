# MAOS Runtime

MAOS is a protocol-first, evidence-first multi-agent collaboration runtime MVP. It turns a software-delivery request into structured tasks, artifacts, evidence, review, approval, and an auditable state history.

This repository implements the first local simulator from the MAOS proposal. It is deliberately deterministic and does not call real models, MCP servers, payment systems, CI, repositories, or production environments.

## What it demonstrates

- Six constrained identities: Manager, Requirement, Architecture, Coding, Testing, and Reviewer.
- A Control Plane as the only component allowed to own state transitions and complete a task.
- Versioned task, event, artifact, evidence, and approval contracts validated at runtime.
- Event idempotency and stale-version rejection.
- Independent-review evidence, bounded rework, and a human-approval pause for high-risk work.
- A simulated refund-approval scenario; amounts above 5,000 require an approval decision.

## Lifecycle

```text
CREATED -> PLANNED -> ASSIGNED -> RUNNING -> VALIDATING -> REVIEW_PENDING
                                                          |             |
                                                          |             +-> APPROVED -> COMPLETED
                                                          +-> REWORK -> FAILED

REVIEW_PENDING -> WAITING_APPROVAL -> APPROVED -> COMPLETED
```

## Agent boundaries

| Role | Output | Boundary |
| --- | --- | --- |
| Manager | Plan DAG and scheduling events | Cannot complete tasks or make external writes. |
| Requirement | Requirements and acceptance criteria | Does not invent business rules. |
| Architecture | API, audit, idempotency, and rollback contract | Does not modify code. |
| Coding | Simulated change manifest | No production or external write. |
| Testing | Replayable test report | Cannot declare completion. |
| Reviewer | Independent review decision | Cannot self-approve. |

## Run locally

Node.js 20+ and pnpm are required.

```bash
pnpm install
pnpm test
pnpm typecheck
pnpm demo
```

`pnpm demo` writes a redacted result to `artifacts/run-result.json`. Generated evidence is intentionally ignored by Git.

## Python parallel skeleton

The original zero-dependency Python contract skeleton is preserved as a separately classified implementation under [`python/`](python/README.md). Run it from that directory:

```bash
cd python
python3 main.py
python3 tests/test_contracts.py
```

## Safety boundary

This is an MVP simulator, not a production payment or agent-execution system. It contains no customer data, credentials, production configuration, network-based tool access, or irreversible action. A real deployment must replace the in-memory adapters with governed infrastructure, enforce tenancy, retain audit records according to policy, and perform independent security review.

## Roadmap

The contracts and adapter seams are designed for later RocketMQ, PostgreSQL, gateway, observability, and human-approval integrations. Those integrations are intentionally out of scope for this initial release.

## License

Licensed under [Apache-2.0](LICENSE).
