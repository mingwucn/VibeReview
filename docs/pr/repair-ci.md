## Summary

- Keep sandbox CLI tests hermetic across Python 3.11 through 3.13.
- Preserve the distinction between a hosted capability diagnostic and actual
  machine-local sandbox qualification.
- Reject fork pull-request code before assignment to a self-hosted runner.
- Document a disposable, credential-free runner and reviewed same-repository
  staging for fork contributions.

No scientific contract or live-engine behavior changes in this pull request.

## Required evidence

- Hosted Python jobs pass on the exact reviewed head.
- Every required sandbox case executes on the dedicated runner and its artifact
  hashes and qualification fingerprint validate.
- Branch protection or a repository ruleset requires the three Python checks
  and `sandbox-qualification` before merge.
- A queued, skipped, neutral, or hosted-only result is not qualification.

Use a normal merge commit after review. Do not bypass a pending check.
