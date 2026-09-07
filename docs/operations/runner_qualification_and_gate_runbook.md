# VibeReview — Sandbox Runner Provisioning and Branch Protection Runbook

**Audience:** Repository and Infrastructure Administrators  
**Date:** September 2026  
**Scope:** Operating instructions for provisioning the self-hosted Linux sandbox runner (`R3`) and configuring the branch protection release gate (`R4`).

---

## 1. Work Package R3 — Self-Hosted Sandbox Runner Provisioning

### 1.1 Purpose and Security Posture

The VibeReview sandbox qualification job (`sandbox-qualification` in `.github/workflows/tests.yml`) verifies OS-level namespace confinement using Bubblewrap (`bwrap`). GitHub-hosted Ubuntu runners run in containerized environments where unprivileged user namespaces and mount flags may be restricted or variable. Therefore, qualification is **host-specific** and must be performed on a dedicated Linux runner matching the target execution environment.

**Mandatory Security Invariants:**
1. **Dedicated Worker:** Use a dedicated, disposable Linux VM or isolated container instance. Never use a personal workstation, development machine, or host containing private keys, live review artifacts, or unredacted credentials.
2. **Runner Account:** The GitHub Actions runner daemon must execute under an unprivileged user account (e.g. `runner` or `actions-runner`). Never run the agent or runner as `root`.
3. **AppArmor & Namespace Policy:** Do not disable AppArmor, SELinux, or kernel security modules globally to force checks to pass. Bubblewrap must operate within standard unprivileged user namespace permissions:
   ```bash
   # Ensure user namespaces are enabled in the kernel
   sysctl kernel.unprivileged_userns_clone
   # Expected output: 1 (or enabled via AppArmor profile on Ubuntu 24.04+)
   ```
4. **Untrusted Code Isolation:** Runner access must be restricted to authorized repository workflows. Do not permit pull requests from untrusted forks to execute arbitrary code on persistent self-hosted runners.

### 1.2 Runner Registration Checklist

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

The repository `mingwucn/VibeReview` is a private repository. In GitHub:
- Branch protection rules and GitHub Rulesets on private repositories may require a **GitHub Team** or **GitHub Enterprise** plan. On a GitHub Free plan for private repositories, API calls to rulesets or branch protection return `403 Forbidden` (`Plan does not support this feature`).

**Administrator Instructions:**
1. Navigate to repository **Settings -> Branches** (or **Settings -> Rules -> Rulesets**).
2. Check if branch protection rules / rulesets are configurable on the `master` branch.
3. **DO NOT make the repository public** as a workaround to gain access to free public branch protection.
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
