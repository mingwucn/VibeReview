# VibeReview — Post-r5h Repair and Follow-up Plan

Prepared: 7 September 2026
Inspected baseline: `ce97ce45ff3c2448d72d37824b10f4cbb99a19f2` (`r5h`).

## 1. Decision and evidence boundary

Do not repeat the B1/B2 implementation or rebuild the B3 runtime. The current repository has advanced beyond the previous `d7c4744` inspection. Its handoffs describe implemented process quiescence, all-entry writable quotas, a trusted resource-limit launcher, credential leases, a Bubblewrap backend, report-derived machine-local qualification, and idempotent accepted-task receipts.

The immediate assignment is to repair deterministic CI, obtain actual sandbox CI evidence, and complete the administrative merge gate. No real LLM adapter should be implemented in that assignment.

Verified from the latest branch/job reads:

- Current master: `ce97ce45ff3c2448d72d37824b10f4cbb99a19f2`.
- Workflow run: `34052665129`.
- Python 3.11 job `101538940911`: 2 failed, 726 passed, 19 deselected.
- Python 3.13: cancelled job conclusion; its pytest step also reported failure.
- Python 3.12: cancelled; not evidence of a pass.
- `sandbox-hosted-capability-check`: successful; this is not sandbox qualification.
- `sandbox-qualification`: queued, with no runner assigned in the retrieved job response.
- The master branch response reports `protected: false`.
- The rulesets read returned HTTP 403 with a private-repository plan-availability message. An administrator must verify available protection features; do not make the repository public as a workaround.

The handoff's `747 passed` is a reported development-host result, not a verified green CI result for this commit. No local checkout was executed while preparing this plan.

The two failures are:

1. `tests/runtime/test_sandbox_qualification.py::test_cli_qualify_success_and_artifacts`
2. `tests/runtime/test_sandbox_qualification.py::test_cli_status_without_qualification`

Both reach a real Bubblewrap backend constructor and return CLI exit 5 with `bwrap executable not found; OS_SANDBOX requires bubblewrap`.

## 2. Work package R1 — repair hermetic CLI unit tests

**Proposed commit:** `r5i Fix sandbox CLI unit-test isolation`.
**Owner:** coding agent.
**Primary files:**

- `tests/runtime/test_sandbox_qualification.py`
- `tests/helpers/conformance_fixtures.py`, only if shared fixture construction is needed
- `src/vibereview/runtime/sandbox.py`, only if a small backend-construction seam materially simplifies testing

### Diagnosis

`_patch_cli()` replaces the probe, conformance runner, and fingerprint function. It does not replace `sandbox_cli.BubblewrapExecutionBackend`. The CLI still constructs that real backend in its successful `qualify` and usable-host `status` paths. A development machine with bwrap masks the incomplete mocking; the ordinary hosted matrix exposes it.

### Required implementation

Patch the constructor at the lookup location used by the CLI. A minimal test-local backend stub should accept the constructor arguments and preserve the network-policy value needed by assertions. It must not discover executables, create namespaces, or execute a subprocess.

Keep real production backend construction unchanged unless a narrow injectable factory is necessary. Do not introduce a new dependency-injection framework.

Do not solve this failure by installing bwrap in every deterministic job, changing expected exit 5 to success, weakening qualification, or reclassifying these unit tests as sandbox integration tests.

Test `UNAVAILABLE`, `BLOCKED`, failed conformance, successful synthetic conformance, unmatched stored qualification, and unexpected internal failure independently. Fabricated reports belong in test helpers only.

### Acceptance tests

- The two failing tests pass when bwrap discovery is deliberately unavailable.
- Deterministic qualification/status tests never invoke the real backend constructor or any host probe/subprocess that was supposed to be mocked.
- Unavailable and blocked probes do not construct a backend or create a qualification record.
- Failed conformance does not issue a qualification.
- A valid synthetic report exercises artifact writing and store lookup through real serialization code in a test-owned store.
- Existing `requires_bwrap` conformance tests continue using real Bubblewrap and executed reports.

Run:

```bash
python -m pytest tests/runtime/test_sandbox_qualification.py \
           -m "not requires_bwrap and not external_engine" -vv

           python -m pytest \
               -m "not external_engine and not requires_bwrap"
               ```

               At the unchanged selection, 728 selected cases should pass; added regressions increase that number. Report observed counts rather than hard-coding a desired total.

## 3. Work package R2 — repair CI evidence and failure visibility

               **Proposed commit:** `r5j Make pre-Codex CI independently diagnosable`.
               **Owner:** coding agent.
               **Primary files:** `.github/workflows/tests.yml`, a small result-checking helper if needed, and its deterministic tests.

### Deterministic matrix

               Retain Python 3.11–3.13. Add `strategy.fail-fast: false` so one failure does not cancel the other diagnostic runs. Add a finite job timeout. Keep the ordinary matrix independent of bubblewrap and real-model credentials.

               Write JUnit XML per Python version. Upload test reports with an unconditional diagnostic-upload step so failures remain inspectable. Use unique artifact names identifying Python version and the workflow attempt.

               Set minimum GitHub token permissions and disable persisted checkout credentials when subsequent git authentication is unnecessary.

### Sandbox qualification evidence

               Use a fresh artifact directory and qualification store for each workflow attempt. Configure `VIBEREVIEW_QUALIFICATION_DIR` to a run-owned directory outside the review project. Never let a persistent runner reuse qualification files from a previous checkout as this run's evidence.

               The qualification job must perform:

               ```bash
               python -m vibereview.runtime.sandbox probe --json --output-dir "$ARTIFACT_DIR"

               python -m pytest \
                   -m "not external_engine and (requires_bwrap or sandbox_conformance)"

                   python -m vibereview.runtime.sandbox qualify \
                       --network-policy deny --json --output-dir "$ARTIFACT_DIR"
                       ```

                       Always retain available probe and conformance diagnostics after a failed step. A successful qualification artifact may be published as such only when qualification issuance succeeds and its report validates. Diagnostic upload is not an alternate success path.

                       Generate a small CI evidence index, separate from scientific objects, containing the tested SHA, workflow run/attempt, runner identity, network policy, suite version, report hash, qualification fingerprint, selected/passed/failed/skipped counts, and artifact hashes. Verify required case coverage with the existing conformance validation functions.

### Hosted capability-check correction

                       The current workflow's usable-probe branch does not validate `qualify_rc`. Make the result combinations explicit:

                       - Usable probe and successful conformance: capability check may pass; its artifact is not transferable runtime qualification.
                       - Unavailable/blocked probe: the documented unavailable/blocked/conformance-failed exit must be returned and no fresh qualification artifact may exist.
                       - Internal error, unexpected exit code, or a conformance failure after a usable probe: fail the job.

                       Keep separate names for capability checks and qualification. Use an isolated temporary store and clean it after the hosted check. Add deterministic tests for the return-code table.

### Acceptance

                       All three hosted Python jobs complete independently. Failure artifacts are present when tests fail. The hosted capability check cannot turn an internal error into success. A qualification pass requires a freshly executed, hash-verified report, not only a job name or an existing JSON file.

## 4. Work package R3 — provision and qualify the self-hosted runner

                       **Owner:** repository/host administrator. Coding agent prepares workflow and runbook changes but must not claim to provision infrastructure without performing it.

                       The retrieved job requests all three labels:

                       ```yaml
                       runs-on: [self-hosted, linux, vibereview-sandbox]
                       ```

                       The job is queued with no assigned runner. That does not identify whether the cause is missing registration, an offline service, a label mismatch, runner-group access, or capacity. Inspect the repository runner configuration and runner service logs before choosing a repair.

### Administrator actions

                       1. Confirm a runner is registered, online, authorized for this repository, and matches all requested labels.
                       2. Use a dedicated clean Linux worker, preferably disposable per job, not a research workstation containing PDFs, SSH keys, personal credentials, or live review state.
                       3. Prepare bubblewrap and the required namespace permissions under a documented host policy. Do not disable AppArmor or other host protections globally to force a green check.
                       4. Run the existing probe as the same non-root account used by the Actions service. Retain its exact diagnostics. The earlier suspected hosted-userns cause is not a diagnosis of the current missing-bwrap unit-test failure.
                       5. Run conformance and qualification on the worker's checkout of the candidate SHA.
                       6. Produce all required CI artifacts and confirm every required conformance case actually ran. A skipped required case is not a qualification pass.
                       7. Re-run the qualification job on the repaired commit. If no suitable host is available, keep the gate blocked and report the infrastructure requirement explicitly.

                       GitHub runner isolation must cover the entire checked-out test suite and workflow, not just the subprocess sandbox used by VibeReview. Restrict who may execute code on this runner; do not execute unreviewed pull-request code on a persistent host with valuable credentials. No LLM credentials are needed for this milestone.

                       A qualification from CI proves the code on that worker. It does not authorize another machine. Each production host must issue its own current matching qualification.

## 5. Work package R4 — protect the branch and close the release gate

                       **Owner:** repository administrator, supported by a coding-agent runbook.

                       First verify whether the private repository's current GitHub plan supports branch protection/rulesets. The inspected branch reports unprotected, and the rulesets endpoint returned a plan-related 403. Preserve repository privacy. If the feature is unavailable, document the blocker rather than claiming enforcement or silently substituting a manual convention.

                       After the repaired checks succeed, require at least these exact contexts:

    ```text
    pytest (3.11)
    pytest (3.12)
pytest (3.13)
    sandbox-qualification
    ```

    Require pull-request review for protected changes, disallow force pushes/deletion, and restrict bypass permissions. Protect changes to CI configuration and the sandbox/qualification tests as well as production runtime code.

    For the initial implementation, run the required checks for all relevant pull requests instead of adding path-filter exceptions. A skipped job, neutral result, old SHA, or documentation-only marker must not be interpreted as sandbox evidence. If conditional skipping is introduced later, add an always-executed aggregate gate that verifies actual required-job success and report coverage.

    Do not run untrusted checkout code through a privileged `pull_request_target` workflow as a shortcut. Runner registration/protection are administrative work items, not LLM semantic tasks.

### Closure record

    Add `docs/handoffs/r5i-r5k-ci-closure.md` with:

    - inspected and tested SHAs;
    - exact unit-test failure and correction;
    - observed local and CI counts separately;
    - deterministic matrix run identifiers;
    - executed sandbox report and fingerprint;
    - runner configuration evidence without secrets;
    - branch-protection evidence or unresolved availability blocker;
    - explicit statement that no live model has been run.

    Update README/HANDOFF status only from the evidence available. Historical handoffs remain historical; do not rewrite old counts as current results.

## 6. Regression-only confirmation of earlier repairs

    Do not reimplement the completed runtime. Re-run existing tests and add a case only where a demonstrated gap remains:

    | Area | Required invariant |
    |---|---|
    | Process quiescence | No proposal is imported while controlled descendants remain active. |
    | Writable quotas | All directory entries count; scans do not follow unsafe links. |
    | Trusted launcher | Requested supported limits are applied; unsupported enforcement is explicit. |
    | Credential leases | Secrets are redacted; every execution/exception path cleans staged credentials. |
    | Receipts | Identical accepted input reuses results without new IDs or a new generation. |
    | Receipt identity | Unrelated tasks of the same type do not reuse each other's result. |
    | Changed semantics | Identical inputs with changed evaluation semantics require explicit reevaluation. |
    | Receipt integrity | Altered payload/hash/object references cannot be reused. |
    | Qualification | Changed host, backend executable, policy, code, or report invalidates qualification. |
    | Scientific outcomes | REJECT, UNCLEAR, and UNSUPPORTED never trigger engine fallback. |

    The earlier suite counts are not acceptance quotas. Failing tests are evidence to investigate, not justification for deleting assertions.

## 7. Release gate before live Codex

    The gate passes only when:

    - the exact candidate commit has successful Python 3.11, 3.12 and 3.13 jobs;
    - the dedicated sandbox job has run, not merely queued or skipped;
    - its report covers every required case and matches its qualification;
    - the ordinary matrix passes without a bwrap dependency;
    - the hosted capability check rejects unexpected failures;
    - branch/ruleset enforcement is actually enabled under the accepted contract;
    - the intended execution host has its own current policy-matching qualification;
    - no real engine has been enabled during the repair work.

    This is an engineering release gate. It does not certify scientific correctness.

## 8. Follow-up after closure — one live Codex task

    Implement a thin `CodexEngine` only after the gate above. Reuse the existing runner, credential lease, diagnostics, qualification and receipts. Do not add another orchestration layer.

    First validate the installed client's non-interactive interface, structured-output mechanism, output handling and credential lookup. Record its version and nonsecret configuration. Construct argv without shell interpolation.

    Qualify the exact network policy needed by that adapter. A DENY qualification is not interchangeable with HOST. Remote model transport and optional agent web-browsing/tool access are distinct permissions. A HOST profile is not an endpoint allowlist; do not represent it as such. Use an explicitly authorized transport policy and disable unrelated browsing/tools. Do not silently broaden permissions to make authentication work.

    Enable only `ASSESS_CLAIM`. Verify that its bundle includes the requested candidate and complete intended publication-level evidence set, and that the proposal's claim reference equals the requested claim rather than merely any existing claim.

    Use four controlled fixtures: RETAIN; NARROW; REJECT/insufficient_evidence; REJECT/contradicted. Separate DTO/runtime conformance from scientific fixture evaluation. An unexpected but structurally valid scientific decision is a finding to inspect, not a trigger to shop for another model's answer.

    Acceptance: a safe task is executed, a ClaimAssessment is committed, negative results are retained without fallback, credentials and descendants are cleaned up, and an identical accepted task reuses its receipt without new IDs. Live tests remain `external_engine` and outside ordinary CI.

## 9. Scientific follow-up sequence

    | Stage | Deliverable | Acceptance |
    |---|---|---|
    | Project shell + resources | One-command initialization/status/run; immutable Markdown CAS | Repeat run changes no source identities or accepted outputs. |
    | Deep Research ingestion | Themes, claims, terminology, paper candidates, controversies and gaps | Discovery remains separate from scientific evidence; every item retains source-resource provenance. |
    | Corpus challenger | Paper sketches, batched comparison, omitted themes/claims | Recovers a deliberately omitted concept before retrieval queries are finalized. |
    | Retrieval | Multi-intent queries and Graphify proposals | Invalid backend output stays in a runtime ledger; verified spans receive canonical IDs and exactly one RetrievalDisposition. |
    | Evidence and claims | Assessed spans → EvidenceRecord → ClaimPaperEvidence → assessment → final validation → ClaimPacket | Exact source offsets, paper-level weighting, contradictions and rejected claims preserved. |
    | Writing | Propositions → audits → rendered sentences → final audits → deterministic assembly | No new generative transformation after the final semantic boundary. |
    | Five-paper pilot | One coherent review section from 2–3 DR reports and 5 paper Markdown files | All citations and claims resolve; hidden-theme challenger fixture passes. |
    | Later expansion | Kimi, Agy, optional OpenCode; PDF parsing and 20–30-paper pilot | Same contracts and conformance tests, without scientific-model redesign. |

    Deep Research remains website-generated and manually supplied. PDF parsing, Elsevier integration, figure generation and large-corpus operation are not prerequisites for the Markdown pilot.

## 10. Immediate copy-paste coding-agent assignment

    Implement the post-r5h CI-closure patch from inspected baseline `ce97ce45ff3c2448d72d37824b10f4cbb99a19f2`. Read the current HEAD first; preserve any newer completed work. Do not implement a real LLM engine.

    1. Reproduce the two failing deterministic sandbox CLI tests. `_patch_cli()` currently mocks probe/conformance/fingerprint functions but leaves `sandbox_cli.BubblewrapExecutionBackend` real. Stub backend construction at the CLI lookup location, or add a minimal testable factory. Preserve real production qualification checks.
    2. Add regressions proving unit qualification/status tests work with bwrap absent and make no unintended OS calls. Keep real conformance tests under `requires_bwrap`.
    3. Update the matrix to `fail-fast: false`, finite timeouts, per-version JUnit XML and unconditional diagnostic uploads. Do not install bwrap in ordinary jobs to hide the test-isolation error.
    4. Fix hosted capability result checking: usable probe must not mask qualification failure; internal exit 5 must fail; unavailable/blocked cases require documented failure codes and no fresh qualification artifact.
    5. Use per-run artifact and qualification-store directories. Retain failure diagnostics without publishing unsuccessful qualification as a pass. Record tested SHA, case coverage, policy and report hashes.
    6. Re-run the existing quiescence, writable-entry, trusted-launcher, credential-lease, receipt and qualification regressions. Do not rebuild those components.
    7. Prepare a runner/protection administrator runbook. The current sandbox job is unassigned; diagnose registration, service, labels, access and capacity rather than assuming the cause. Keep a non-available runner or unavailable branch-protection feature as an explicit blocker.
    8. Update README/HANDOFF with separate reported-local, verified-CI and host-qualification status. Add the CI-closure handoff.

    Stop after code/tests/workflow/runbook changes. Do not modify repository privacy, spend money, provision a runner, enable real-model credentials or change branch settings without the required administrator authorization. Do not claim remote CI or protection passed unless verified.

    Return changed files, root cause, tests added, exact commands and results, current-SHA CI evidence, remaining administrative blockers, and confirmation that no scientific models or real engines were added.

## Evidence map

    Repository: `mingwucn/VibeReview`, baseline `ce97ce45ff3c2448d72d37824b10f4cbb99a19f2`.

    Primary inspected sources:

    - branch metadata for `master`;
    - `HANDOFF.md`;
    - `docs/handoffs/r5-pre-real-engine.md` (historical implementation record);
    - `docs/handoffs/r5e-r5h-sandbox-qualification.md`;
    - `src/vibereview/runtime/sandbox.py`;
    - `tests/runtime/test_sandbox_qualification.py`;
    - `.github/workflows/tests.yml`;
    - workflow `34052665129`, jobs and Python 3.11 log `101538940911`.

    Planning background: supplied council reviews preserve the frozen scientific architecture, mandatory corpus challenger, RetrievalDisposition, private task bundles and separation of live-engine qualification from ordinary CI. Those reviews did not independently verify the current repository.

    External operational references consulted: GitHub Docs, “Self-hosted runners reference”, “Secure use reference”, and “Troubleshooting required status checks”; OpenAI official “Non-interactive mode” documentation. Repository-specific findings above come from connected GitHub reads, not public web search.

