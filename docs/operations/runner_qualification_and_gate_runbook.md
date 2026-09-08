# VibeReview — Sandbox Runner Provisioning and Branch Protection Runbook

**Audience:** Repository and Infrastructure Administrators  
**Date:** September 2026  
**Scope:** Operating instructions for provisioning the self-hosted Linux sandbox runner (`R3`) and configuring the branch protection release gate (`R4`).

---

## 1. Work Package R3 — Self-Hosted Sandbox Runner Provisioning

### 1.1 Purpose and Security Posture

The VibeReview sandbox qualification job (`sandbox-qualification` in `.github/workflows/tests.yml`) verifies OS-level namespace confinement using Bubblewrap (`bwrap`). GitHub-hosted Ubuntu runners run in containerized environments where unprivileged user namespaces and mount flags may be restricted or variable. Therefore, qualification is **host-specific** and must be performed on a dedicated Linux runner matching the target execution environment.

**Mandatory Security Invariants:**
1. **Dedicated Worker:** Use a dedicated, single-job disposable Linux VM or isolated container instance and destroy or reimage it after the job. Never use a personal workstation, development machine, or host containing private keys, live review artifacts, or unredacted credentials.
2. **Runner Account:** The GitHub Actions runner daemon must execute under an unprivileged user account (e.g. `runner` or `actions-runner`). Never run the agent or runner as `root`.
3. **AppArmor & Namespace Policy:** Do not disable AppArmor, SELinux, or kernel security modules globally to force checks to pass. Bubblewrap must operate within standard unprivileged user namespace permissions:
   ```bash
   # Ensure user namespaces are enabled in the kernel
   sysctl kernel.unprivileged_userns_clone
   # Expected output: 1 (or enabled via AppArmor profile on Ubuntu 24.04+)
   ```
4. **Untrusted Code Isolation:** Runner access must be restricted to authorized repository workflows. The workflow's job-level gate permits `sandbox-qualification` only for pushes and same-repository pull requests, so fork code is rejected before runner assignment and checkout.
5. **Credential-Free Jobs:** Do not configure repository secrets, production API keys, SSH keys, personal tokens, or research credentials on the runner. Keep only the minimum ephemeral GitHub runner authorization needed to accept the job; the workflow itself uses a read-only `GITHUB_TOKEN` and does not persist checkout credentials.

### 1.2 Fork Pull Requests and Trusted Staging

GitHub-hosted `pytest` and `sandbox-hosted-capability-check` jobs continue to run for fork pull requests. The self-hosted qualification job is intentionally skipped for those events; a skipped fork job is not qualification evidence.

After the hosted checks pass, a maintainer may qualify a fork contribution only through this trusted staging procedure:

1. Review the exact fork commit and its complete diff, with special attention to workflow files, test collection, dependency installation, and executable scripts.
2. Reproduce the reviewed changes on a new branch in `mingwucn/VibeReview`, based on the intended target. Record the fork commit and resulting staging commit; do not add unrelated changes.
3. Open a same-repository staging pull request. Its `sandbox-qualification` job may use the self-hosted runner because the trusted branch is owned by this repository.
4. Require the hosted matrix and executed sandbox qualification for the staging commit. If the fork changes, discard the old qualification, repeat review and staging, and run the checks again.
5. Merge the reviewed staging pull request rather than treating a check from a different SHA as authority for the original fork pull request.

Do not use `pull_request_target` to execute fork code, manually checkout a fork ref in the qualification job, or attach credentials to make the fork workflow run on the self-hosted runner.

### 1.3 Runner Registration Checklist

1. **Host Prerequisites:**
   - Linux x86_64 (Kernel ≥ 5.15 recommended).
   - Python 3.11, 3.12, and 3.13 installed (e.g. via system packages or pyenv).
   - Bubblewrap (`bwrap`) installed:
     ```bash
     sudo apt-get update && sudo apt-get install -y bubblewrap
     which bwrap
     bwrap --version
     ```
   - Standard build and test tools: `git`, `python3-venv`, `gcc`, `make`.

2. **Actions Runner Configuration:**
   - Download and configure the GitHub Actions runner package.
   - When registering with `config.sh`, specify the exact labels requested by `.github/workflows/tests.yml`:
     ```text
     self-hosted, linux, vibereview-sandbox
     ```
   - Verify runner group assignment: Ensure the runner is assigned to a group that has access to `mingwucn/VibeReview`.
   - Start the service:
     ```bash
     sudo ./svc.sh install
     sudo ./svc.sh start
     sudo ./svc.sh status
     ```

3. **Pre-flight Local Verification:**
   Before unpausing or accepting workflow runs, log in as the runner service user, checkout the candidate SHA, and verify capability probing locally:
   ```bash
   python -m vibereview.runtime.sandbox probe --json
   ```
   **Acceptance:** Exit code `0` (`usable`). If exit code is `2` (`unavailable`) or `3` (`blocked`), inspect `stderr` and command diagnostics for user namespace or mount permission denials.

4. **Run Local Conformance and Qualification:**
   ```bash
   python -m pytest -m "not external_engine and (requires_bwrap or sandbox_conformance)"
   python -m vibereview.runtime.sandbox qualify --network-policy deny --json --output-dir /tmp/test-artifacts
   python -m vibereview.runtime.sandbox status --json
   ```
   **Acceptance:** Qualification is successfully issued, matches the current host fingerprint, and status reports `qualification_match: true`.

---

## 2. Work Package R4 — Branch Protection and Merge Gate

### 2.1 GitHub Plan Availability Verification

Verify current repository visibility and protection state before applying the gate. Private-repository protection may depend on the account plan; a public repository must have protection configured before accepting external contributions. A visibility change never substitutes for configuring the checks below.

**Administrator Instructions:**
1. Navigate to repository **Settings -> Branches** (or **Settings -> Rules -> Rulesets**).
2. Check if branch protection rules / rulesets are configurable on the `master` branch.
3. Do not change repository visibility solely to bypass an unavailable protection feature. If publication is an independently approved project decision, apply the public-repository fork isolation and trusted-staging procedure in §1.2 before accepting contributions.
4. If plan features are unavailable:
   - Explicitly document the plan blocker in the project handoffs (`docs/handoffs/r5i-r5k-ci-closure.md`).
   - Treat CI checks as mandatory administrative preconditions manually verified before merging any pull request.

### 2.2 Required Status Check Configuration

When branch protection or rulesets are enabled, configure protection for `master` with:

1. **Require pull request before merging:**
   - Require at least 1 approving review.
   - Dismiss stale approvals when new commits are pushed.
2. **Require status checks to pass before merging:**
   - Require branches to be up to date before merging.
   - Specify the following exact status check contexts:
     ```text
     pytest (3.11)
     pytest (3.12)
     pytest (3.13)
     sandbox-qualification
     ```
   - Note: Do NOT require `sandbox-hosted-capability-check` as a qualification equivalent; that check is a fail-closed capability assertion on hosted runners.
3. **Disallow bypasses:**
   - Do not allow bypass of above settings by administrators unless an emergency break-glass procedure is formally recorded.
4. **Prevent destructive updates:**
   - Disallow force pushes.
   - Disallow branch deletion.
