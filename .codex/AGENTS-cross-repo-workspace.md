# Cross-repository workspace instructions

Place a customized copy of this file as `AGENTS.md` in a narrow parent directory
that contains the related repositories, then open that parent directory as the
VS Code/Codex workspace.

## Workspace repositories

- `repo-a/` — `<purpose>`
- `repo-b/` — `<purpose>`
- `shared-contracts/` — `<purpose, if applicable>`

## Relationship map

- `repo-a` depends on `repo-b` through `<package/API/schema/protocol>`.
- The authoritative contract is `<repository/path/artifact>`.
- Generated artifacts are produced by `<command/workflow>` and consumed by
  `<repository/path>`.

## Startup procedure

- Determine which repositories are relevant to the request.
- Read each relevant repository's own `AGENTS.md` and follow its instructions.
- Record each repository's branch, commit SHA, working-tree status, manifests,
  lockfiles, and relevant dependency versions.
- Do not assume similarly named branches or tags correspond across repositories.

## Analysis behavior

- Default to read-only analysis unless the user explicitly requests edits.
- Trace behavior across repository boundaries end-to-end.
- Cite evidence from every repository that materially supports a conclusion.
- Distinguish checked-in source, generated output, published packages, deployed
  revisions, and current upstream documentation.
- When public documentation is time-sensitive, search the web using official or
  primary sources and reconcile it with the versions pinned locally.
- Never send private source code, secrets, customer data, or internal identifiers
  in a web query.

## Cross-repository changes

- Modify multiple repositories only when the user explicitly authorizes it.
- Keep changes separated by repository and explain required sequencing.
- Validate each repository independently, then run integration/contract checks.
- Report any repository that could not be validated and the exact blocker.
