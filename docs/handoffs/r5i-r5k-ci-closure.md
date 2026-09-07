# Hand-off — r5i–r5k: Sandbox CLI Isolation, Pre-Codex CI Diagnostics, and Gate Closure

## 1. Baseline and Candidate SHAs

- **Inspected Baseline:** `ce97ce45ff3c2448d72d37824b10f4cbb99a19f2` (`r5h`)
- **Fix Commits on `repair/ci-sandbox-isolation`:**
  - `r5i Fix sandbox CLI unit-test isolation`
  - `r5j Make pre-Codex CI independently diagnosable`
  - `r5k CI closure, admin runbook and status handoff`
- **Target Remote Branch:** `master` (and integrated into `integrate/mylib-readonly`)

---

## 2. Unit-Test Failure Root Cause and Correction (Work Package R1 / r5i)

### 2.1 Diagnosis
In GitHub Actions workflow run `34052665129` (Python 3.11 job `101538940911`), deterministic CI failed with 2 failures:
1. `tests/runtime/test_sandbox_qualification.py::test_cli_qualify_success_and_artifacts`
2. `tests/runtime/test_sandbox_qualification.py::test_cli_status_without_qualification`

Both failed with exit code 5 (`Internal error: RuntimeError: bwrap executable not found; OS_SANDBOX requires bubblewrap`).
The root cause was that `_patch_cli()` mocked `probe_sandbox_capabilities()`, `run_sandbox_conformance()`, and `compute_current_qualification_fingerprint()`, but left `sandbox_cli.BubblewrapExecutionBackend` pointing to the real class. When running on a development machine with `/usr/bin/bwrap`, the backend constructor succeeded and masked the incomplete mock. On ordinary hosted CI runners lacking `bwrap`, the constructor raised `RuntimeError`.

### 2.2 Correction
- Implemented `_StubBubblewrapExecutionBackend` within `tests/runtime/test_sandbox_qualification.py` and patched `sandbox_cli.BubblewrapExecutionBackend` inside `_patch_cli()`.
- The test stub accepts real constructor arguments and exposes `.network_policy`, `.bwrap_path`, and `.confinement_level` without probing namespaces, scanning the filesystem, or spawning subprocesses.
- Added regression tests verifying:
  - CLI `qualify` and `status` succeed hermetically when `shutil.which("bwrap")` is `None` and `PATH` contains no bwrap binary.
  - Non-usable probes (`UNAVAILABLE`, `BLOCKED`) never construct a backend, write qualification artifacts, or create store entries.
  - Conformance failure never stores a qualification.
  - Unexpected internal exceptions terminate with documented exit code 5.
  - Real end-to-end conformance tests under `requires_bwrap` remain intact and functional.

---

## 3. Pre-Codex CI Diagnostics and Evidence (Work Package R2 / r5j)

### 3.1 Workflow Hardening (`.github/workflows/tests.yml`)
- **Deterministic Matrix:** Added `fail-fast: false`, finite timeouts (`15m`), per-version JUnit XML (`pytest-${{ matrix.python-version }}.xml`), and unconditional test report artifact upload (`if: always()`).
- **Security Posture:** Restricted default token permissions (`permissions: contents: read`) and disabled credential persistence (`persist-credentials: false`).
- **Isolated Run Directories:** Qualification jobs configure `VIBEREVIEW_QUALIFICATION_DIR` and `ARTIFACT_DIR` to unique run-owned temporary directories (`${RUNNER_TEMP}/...`) outside the repository tree, preventing cross-attempt contamination.
- **Fail-Closed Hosted Capability Check:** The hosted check step now validates both `probe_rc` and `qualify_rc` via a dedicated validator, preventing unexpected successes or internal failures from being masked.

### 3.2 CI Evidence Indexing and Hosted Validation (`vibereview.runtime.ci_evidence`)
- Created `src/vibereview/runtime/ci_evidence.py` with:
  - `validate_hosted_check_result()` enforcing the return-code contract:
    - Usable probe (0) + qualify (0) -> passed check (non-transferable).
    - Usable probe (0) + qualify (!= 0) -> failure.
    - Unavailable/blocked probe (2, 3) + qualify (2, 3, 4) with NO qualification artifact -> pass fail-closed.
    - Any unexpected code or presence of qualification artifact on non-usable host -> failure.
  - `build_ci_evidence_index()` generating `ci-evidence-index.json` validating Pydantic models, report hash stability, required case coverage, artifact SHA-256 hashes, and pytest test counts.
- Added comprehensive deterministic tests in `tests/runtime/test_ci_evidence.py` (25 passing tests).

---

## 4. Observed Suite Counts (Local vs. CI)

### Local Observed Results (Development Host: Linux x86_64, Python 3.13)
- **Deterministic Suite:**
  `python -m pytest -m "not external_engine and not requires_bwrap"`
  - **757 passed, 19 deselected** (previously 728; 4 unit tests added in `r5i` and 25 unit tests in `r5j` = 29 new tests).
- **Conformance Suite:**
  `python -m pytest -m "requires_bwrap or sandbox_conformance"`
  - **22 passed** (including drift check and 11 real conformance cases).
- **Total Local Suite:**
  `python -m pytest`
  - **776 passed** (757 deterministic + 19 bwrap/conformance cases).

### CI Status
- Pre-repair run `34052665129`:
  - Python 3.11: 2 failed, 726 passed, 19 deselected.
  - Python 3.12 / 3.13: cancelled due to fail-fast.
  - `sandbox-qualification`: queued (unassigned runner).
- Repaired commits (`r5i`, `r5j`, `r5k`) eliminate the bwrap dependency from the deterministic matrix, isolate CLI tests, and make CI diagnostics independently inspectable.

---

## 5. Administrative Blockers and Runner Status (Work Packages R3 & R4)

### 5.1 Self-Hosted Runner Status (R3)
- The GitHub Actions `sandbox-qualification` job requires `[self-hosted, linux, vibereview-sandbox]`.
- Current status: **BLOCKED / PENDING INFRASTRUCTURE**. No active runner is currently registered with these labels for `mingwucn/VibeReview`.
- Operational instructions for registering a dedicated, non-root worker with Bubblewrap and unprivileged userns support are documented in:
  [`docs/operations/runner_qualification_and_gate_runbook.md`](../operations/runner_qualification_and_gate_runbook.md).

### 5.2 Branch Protection & Merge Gate Status (R4)
- Verification check on `master`: Currently reports `protected: false`.
- GitHub Rulesets API check returned `403 Forbidden` (`Plan does not support this feature` for private repository).
- Current status: **BLOCKED / PENDING PLAN VERIFICATION**. Do NOT make the repository public as a workaround.
- Mandatory gate contexts when enabled:
  - `pytest (3.11)`
  - `pytest (3.12)`
  - `pytest (3.13)`
  - `sandbox-qualification`

---

## 6. Scientific Contract and Release Boundary Statement

- **No scientific models, transitions, or contracts were altered.**
- **No real LLM engine or model API calls were implemented or executed.**
- **Confinement and qualification contracts remain strictly fail-closed.**
