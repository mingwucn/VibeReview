> Status: retained review text. The CI repair is present in the private
> `origin/master` root snapshot. GitHub Actions are disabled, and this file is
> not evidence that hosted checks, runner qualification, or protection have
> executed.

## Summary

- Keep sandbox CLI tests hermetic across Python 3.11 through 3.13.
- Preserve the distinction between a hosted capability diagnostic and actual
  machine-local sandbox qualification.
- Reject fork pull-request code before assignment to a self-hosted runner.
- Document a disposable, credential-free runner and reviewed same-repository
  staging for fork contributions.

No scientific contract or live-engine behavior changes in this CI repair.

## Required evidence

- Hosted Python jobs pass on the exact reviewed head.
- Every required sandbox case executes on the dedicated runner and its artifact
  hashes and qualification fingerprint validate.
- Branch protection or a repository ruleset requires the three Python checks
  and `sandbox-qualification` before protected-branch changes are accepted.
- A queued, skipped, neutral, or hosted-only result is not qualification.

Do not treat the repair's presence on master as proof that required checks,
runner qualification, or protection have been executed.
