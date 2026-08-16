# Contributing

Contributions should preserve MAOS's protocol-first, evidence-first, and least-privilege boundaries.

1. Create a focused branch and describe the behavior being changed.
2. Add or update a failing automated test before implementation.
3. Run `pnpm test` and `pnpm typecheck` before submitting changes.
4. Keep simulated adapters free of production credentials, customer data, and network side effects.
5. Do not commit `.env` files, generated `artifacts/run-result.json`, local worktrees, or rendered proposal files.

For security-sensitive behavior, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
