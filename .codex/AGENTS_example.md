# Project instructions

## Priorities

- Make the smallest correct change that satisfies the request.
- Follow nearby patterns before introducing a new abstraction.
- Do not rewrite unrelated files.
- Preserve public behavior unless the task explicitly changes it.

## Commands

- Install: `pnpm install`
- Format: `pnpm format`
- Lint: `pnpm lint`
- Type-check: `pnpm typecheck`
- Test: `pnpm test`
- Build: `pnpm build`

## Implementation rules

- Add or update tests for behavior changes and bug fixes.
- Avoid adding production dependencies without a clear need.
- Do not weaken type checks merely to make the build pass.
- Avoid broad formatting changes that obscure the functional diff.
- Update documentation when commands, APIs, or user-visible behavior change.

## Validation

- Run the narrowest relevant checks first.
- Report every command run and whether it passed.
- Never claim a check passed unless it was actually run.
- Identify pre-existing failures separately from failures caused by the change.

## Security

- Never print, commit, or copy secrets.
- Do not modify `.env` files unless explicitly requested.
- Do not run `git reset --hard`, `git clean -fd`, or force-push.
- Ask before deleting data or performing irreversible migrations.

## Final response

Include:

1. A concise summary.
2. Important files changed.
3. Tests and checks run.
4. Remaining risks or assumptions.