# Hand-off — Milestone A: Task-Resource Boundary (r3)

## Summary

Milestone A implemented the self-contained task-resource boundary and immutable bundle contracts as specified in `goal.md` §6.

- **Commit:** `ca7e1a4` (commit `r3`)
- **Suite status:** 326 tests passed via `python -m pytest`

## What was built

1. **TaskSpec-owned dependencies and resources:**
   - Specifications explicitly declare task dependencies (`manifest.dependencies`) and input resources (`manifest.resources`).
   - Resource snapshots are captured copy-while-hashing at task staging time.

2. **Private invocation DTOs vs sanitized engine inputs:**
   - Tasks accept private, strongly-typed invocation DTOs (`*Invocation`).
   - Input payloads are sanitized into public bundle inputs (`bundle/inputs/`) without exposing private task provenance.

3. **Bundle integrity and trust anchors:**
   - Private provenance and bundle hashes are stored in `work/tasks/<task_id>/private/task_provenance.json`.
   - Per-attempt workspaces are instantiated from the immutable `bundle/` template.
   - Any tampering with the bundle or inputs is detected via bundle integrity preflight.

4. **Resource freshness and commit verification:**
   - At writer-locked commit time, resource and dependency hashes are checked for freshness against the active repository snapshot, preventing stale commits.
