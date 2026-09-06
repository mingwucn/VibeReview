# VibeReview — Detailed Fix and Follow-up Plan

## 1. Current decision

The current `master` head is `3e157d503f94c301c7d0143f69018566245f86c7`, labelled **“r5d Milestone B3: documentation, CI and final pre-Codex gate.”** The ordinary test matrix passes on Python 3.11, 3.12 and 3.13, with 697 deterministic tests selected in each job. The dedicated sandbox-conformance job, however, failed seven of its eleven selected tests.

The correct status is therefore:

```text
Scientific contract                         PASS
Deterministic runtime                       PASS
Task-resource boundary                      PASS
Fake subprocess boundary                    PASS
Process/credential/receipt hardening        PASS
Bubblewrap implementation                   PRESENT
Bubblewrap qualification                    FAIL
Final pre-Codex gate                        NOT PASSED
CodexEngine                                 BLOCKED
```

The scientific architecture should remain frozen. The uploaded council likewise concluded that confinement qualification must be tied to the actual executable, implementation, platform capabilities and successful conformance evidence; a credential or confinement failure must prevent canonicalization; and accepted-task reuse must retain current validation and transition checks.

The immediate objective is not to redesign the runtime. It is to turn the current Bubblewrap implementation from **present but unqualified** into either:

```text
QUALIFIED on this host/profile
```

or:

```text
UNSUPPORTED on this host/profile
```

with no ambiguous middle state.

---

# 2. Likely failure class

All seven execution-based sandbox tests returned `ENGINE_EXECUTION_FAILURE`. Tests that merely examined models or configuration passed. This pattern indicates that the inner fake worker probably did not start successfully inside Bubblewrap, but the current CI assertions do not expose the retained process stderr, final `argv`, or detected technical failures. Consequently, the exact cause has not yet been established from the workflow output.

A plausible environmental explanation is Ubuntu 24.04’s AppArmor-mediated restriction on unprivileged user namespaces. Ubuntu documents that unprivileged applications may require an explicit AppArmor profile to create user namespaces, and Ubuntu 24.04 enables these restrictions by default. The release notes advise application-specific profiles and warn that globally disabling the restriction reduces the intended kernel-exploit mitigation. ([Ubuntu Documentation][1])

    This remains a **hypothesis**, not a confirmed diagnosis. The first repair must therefore improve observability and capability probing before any CI or Bubblewrap flags are changed.

    ---

# 3. Repair sequence

    Use four bounded repair commits before starting CodexEngine:

    ```text
    r5e
    Sandbox observability and capability preflight
    ↓
    r5f
    Bubblewrap command/profile and qualification-host repair
    ↓
    r5g
    Attested qualification artifact, CI and branch gate
    ↓
    r5h
    Receipt correctness, documentation and final pre-Codex audit
    ↓
    r6a
    CodexEngine adapter
    ↓
    r6b
    Four live ASSESS_CLAIM qualifications
    ```

    No Deep Research, Graphify, manuscript or additional-engine implementation should enter `r5e–r5h`.

    ---

# 4. Commit r5e — Sandbox observability and capability preflight

## 4.1 Add a formal probe model

    Create:

    ```python
    class SandboxProbeStatus(StrEnum):
        UNAVAILABLE = "unavailable"
        BLOCKED = "blocked"
        USABLE = "usable"


        class SandboxFailureCode(StrEnum):
            EXECUTABLE_NOT_FOUND = "executable_not_found"
            VERSION_PROBE_FAILED = "version_probe_failed"

            USER_NAMESPACE_DENIED = "user_namespace_denied"
            MOUNT_NAMESPACE_DENIED = "mount_namespace_denied"
            PID_NAMESPACE_DENIED = "pid_namespace_denied"
            NETWORK_NAMESPACE_DENIED = "network_namespace_denied"

            APPARMOR_USERNS_RESTRICTION = "apparmor_userns_restriction"
            PROFILE_EXECUTION_FAILED = "profile_execution_failed"
            UNKNOWN = "unknown"


            class SandboxProbeCommandResult(RuntimeModel):
                name: str
                argv: tuple[str, ...]
                exit_code: int | None
                stdout: str
                stderr: str
                duration_seconds: float


                class SandboxProbeResult(RuntimeModel):
                    backend_name: str
                    backend_version: str | None
                    executable_path: Path | None
                    executable_hash: Sha256 | None

                    status: SandboxProbeStatus
                    failure_code: SandboxFailureCode | None
                    diagnostic: str | None

                    operating_system: str
                    architecture: str
                    kernel_release: str
                    wsl_detected: bool

                    unprivileged_userns_clone: str | None
                    apparmor_restrict_unprivileged_userns: str | None
                    apparmor_profile_detected: bool | None

                    commands: tuple[SandboxProbeCommandResult, ...]
                    ```

                    The probe must contain no secrets and may be persisted as a diagnostic artifact.

                    ---

## 4.2 Probe actual capabilities, not only binary existence

                    The current conformance tests use:

                    ```python
                    HAVE_BWRAP = shutil.which("bwrap") is not None
                    ```

                    as their execution condition. That proves only that a binary is installed. It does not establish that the host permits the namespaces required by the profile.

                    Replace this with staged probing:

### Probe 1 — executable

                    ```bash
                    bwrap --version
                    ```

### Probe 2 — user and mount namespace

                    Run a minimal command using the smallest required user/mount configuration.

### Probe 3 — PID namespace

                    Run a minimal PID namespace probe.

### Probe 4 — network-denied profile

                    Run the network-isolated profile used by `NetworkPolicy.DENY`.

### Probe 5 — complete VibeReview profile

                    Mount a minimal temporary execution tree and run:

                    ```text
                    read bundle
                    write scratch
                    write output
                    exit 0
                    ```

                    Only Probe 5 can produce:

                    ```text
                    status = USABLE
                    ```

                    A binary that exists but fails Probe 2–5 must be:

                    ```text
                    status = BLOCKED
                    ```

                    not “available”.

                    ---

## 4.3 Preserve raw failure diagnostics

                    For every probe command, retain bounded:

                    ```text
                    exit code
                    stdout
                    stderr
                    argv
                    duration
                    ```

                    Known Bubblewrap messages should be mapped to explicit diagnostic categories, for example:

                    ```text
                    "setting up uid map: Permission denied"
                    → USER_NAMESPACE_DENIED

                    "Creating new namespace failed"
                    → USER_NAMESPACE_DENIED or MOUNT_NAMESPACE_DENIED

                    "loopback: Failed RTM_NEWADDR"
                    → NETWORK_NAMESPACE_DENIED

                    AppArmor denial plus restricted-userns sysctl
                    → APPARMOR_USERNS_RESTRICTION
                    ```

                    Unknown messages remain:

                    ```text
                    UNKNOWN
                    ```

                    Do not infer success or qualification from a known error string alone.

                    ---

## 4.4 Improve conformance-test failure output

                    Add a test helper:

                    ```python
                    def assert_sandbox_result_valid(
                            runtime: ProjectRuntime,
                            result: RuntimeResult,
                            ) -> None:
                    if result.outcome is not AttemptOutcome.VALID_SCIENTIFIC_RESULT:
                    record = result.attempt_records[0] if result.attempt_records else None

                    details = {
                        "outcome": result.outcome,
                        "record": (
                                record.model_dump(mode="json")
                                if record is not None
                                else None
                                ),
                        "agent_result": load_agent_result_if_available(...),
                        "sandbox_probe": load_probe_result_if_available(...),
                    }

    pytest.fail(
            json.dumps(details, indent=2, ensure_ascii=False)
            )
    ```

    Every failing conformance test should print:

    ```text
    probe status
    Bubblewrap argv
    process exit code
    retained stderr
    detected failures
    applied limits
    quiescence record
    ```

    This should be implemented before attempting to correct Bubblewrap itself.

    ---

## 4.5 Distinguish two test classes

### Capability-negative tests

    These test that an unsupported environment fails closed.

    They belong in ordinary deterministic CI:

    ```text
    bwrap missing
    → real-engine gate rejects

    bwrap installed but userns blocked
    → real-engine gate rejects

    probe status BLOCKED
    → no qualification generated
    ```

### Positive conformance tests

    These require:

    ```text
    SandboxProbeStatus.USABLE
    ```

    They run only on a host deliberately prepared for Bubblewrap qualification.

    A failed probe must fail the qualification job with a clear report. It must not silently skip the qualification tests and must not generate a qualified artifact.

    ---

## 4.6 r5e tests

    Add:

    ```text
    test_bwrap_binary_absent_is_unavailable

    test_bwrap_binary_present_but_userns_denied_is_blocked

    test_network_namespace_denial_is_classified

    test_full_profile_probe_required_for_usable

    test_blocked_probe_cannot_create_qualification

    test_conformance_failure_prints_stderr_and_detected_failures

    test_probe_report_contains_no_project_private_paths

    test_probe_report_contains_no_credentials
    ```

## r5e acceptance gate

    ```text
    Every sandbox failure is diagnosable from CI output.

    Binary presence is no longer equated with usability.

    Unsupported hosts fail closed without being described as qualified.

    No scientific model changes.
    ```

    ---

# 5. Commit r5f — Repair the Bubblewrap profile and qualification host

## 5.1 Replace opaque `--unshare-all`

    The current backend uses `--unshare-all` for the denied-network profile. Explicit flags are easier to qualify, fingerprint and diagnose.

    Use an explicit profile.

### Common isolation

    ```text
    --unshare-user
    --unshare-pid
    --unshare-ipc
    --unshare-uts
    --unshare-cgroup-try
    --die-with-parent
    --new-session
    ```

### `NetworkPolicy.DENY`

    Add:

    ```text
    --unshare-net
    ```

### `NetworkPolicy.HOST`

    Do not add `--unshare-net`.

    Each required namespace should be represented in the profile model and capability probe.

    ```python
    class SandboxProfile(RuntimeModel):
        user_namespace: bool
        mount_namespace: bool
        pid_namespace: bool
        ipc_namespace: bool
        uts_namespace: bool
        cgroup_namespace: bool
        network_policy: NetworkPolicy
        ```

        The profile hash must be generated from this explicit structure.

        ---

## 5.2 Clear and reconstruct the sandbox environment

        Use Bubblewrap’s environment-clearing facility where supported, then set only required variables.

        Inside the sandbox:

        ```text
        HOME=/work/home
        XDG_CONFIG_HOME=/work/home/.config
        XDG_CACHE_HOME=/work/home/.cache
        TMPDIR=/work/tmp
        PATH=<controlled value>
        LANG=<controlled value>
        LC_ALL=<controlled value, when used>
        ```

        The current command remaps `HOME` and `TMPDIR` but not the XDG variables, while the outer process environment contains host execution-root XDG paths. These should not leak into the sandbox.

        Add an engine probe that prints:

        ```text
        HOME
        XDG_CONFIG_HOME
        XDG_CACHE_HOME
        TMPDIR
        ```

        and verifies that every value begins with `/work/`.

        ---

## 5.3 Preserve the mount boundary

        The sandbox should expose:

        ```text
        /work/bundle        read-only
        /work/launcher      read-only
        /work/output        writable
        /work/scratch       writable
        /work/home          writable
        /work/tmp           writable
        /work/credentials   read-only where feasible
        /proc               sandbox proc
        /dev                minimal device tree
        ```

        It must not expose:

        ```text
        project root
        state/generations
        task private/
        Git working tree
        original input directories
        real HOME
        ```

        System paths required by the interpreter may be mounted read-only.

        ---

## 5.4 Add genuine network tests

        The current HOST test checks only that the backend property equals `NetworkPolicy.HOST`; it does not execute a network operation.

        Use a temporary loopback TCP server created by the test process.

### HOST profile

        ```text
        sandbox worker
        → connects to host loopback server
        → receives known response
        → proposal succeeds
        ```

### DENY profile

        ```text
        sandbox worker
        → attempts same connection
        → connection fails
        → proposal still succeeds
        → diagnostic confirms denial
        ```

        This avoids dependence on the public internet.

        ---

## 5.5 Qualification-host strategy

### Preferred strategy: dedicated qualification runner

        Use a dedicated Linux VM or self-hosted GitHub Actions runner labelled:

        ```text
        self-hosted
        linux
        vibereview-sandbox
        ```

        The runner image should have:

        ```text
        Bubblewrap installed
        supported user namespaces
        required AppArmor profile loaded
        known kernel configuration
        no unrelated credentials
        ```

        This gives a stable qualification environment and a reproducible platform fingerprint.

### Secondary strategy: prepare a hosted runner

        A GitHub-hosted Ubuntu 24.04 runner may be used only if the workflow explicitly installs and loads an application-specific Bubblewrap AppArmor profile and the full profile probe passes.

        Do not automatically disable:

        ```text
        kernel.apparmor_restrict_unprivileged_userns
        ```

        globally merely to make tests pass. Ubuntu recommends application-specific profiles; globally disabling the restriction weakens the security measure it was designed to provide. ([Ubuntu Documentation][2])

        A hosted qualification workflow may attempt:

        ```bash
        sudo apt-get update
        sudo apt-get install -y \
            bubblewrap \
            apparmor \
            apparmor-utils \
            apparmor-profiles
            ```

            Then, when the appropriate profile is available:

            ```bash
            sudo install -m 0644 \
                /usr/share/apparmor/extra-profiles/bwrap-userns-restrict \
                /etc/apparmor.d/bwrap-userns-restrict

                sudo apparmor_parser -r \
                    /etc/apparmor.d/bwrap-userns-restrict
                    ```

                    The workflow must then run the real VibeReview probe. Successful package installation alone is insufficient.

                    ---

## 5.6 r5f tests

                    ```text
                    test_explicit_namespace_profile_matches_profile_hash

                    test_xdg_paths_are_sandbox_local

                    test_host_network_profile_connects_to_loopback_server

                    test_deny_network_profile_cannot_connect

                    test_bundle_and_launcher_are_read_only

                    test_output_and_scratch_are_writable

                    test_credentials_are_minimally_visible

                    test_project_root_is_absent

                    test_task_private_is_absent

                    test_descendants_die_with_sandbox
                    ```

## r5f acceptance gate

                    ```text
                    The actual qualification host reports USABLE.

                    Every execution-based conformance test passes.

                    The exact namespace and environment profile is fingerprinted.

                    No global security restriction is silently disabled.
                    ```

                    ---

# 6. Commit r5g — Attested qualification artifact and CI gate

## 6.1 Add per-case conformance records

                    ```python
                    class SandboxConformanceCaseResult(RuntimeModel):
                        case_id: str
                        passed: bool

                        started_at: str
                        finished_at: str

                        diagnostic: str | None
                        attempt_outcome: AttemptOutcome | None

                        stdout_hash: Sha256 | None
                        stderr_hash: Sha256 | None


                        class SandboxConformanceReport(RuntimeModel):
                            suite_version: str

                            backend_name: str
                            confinement_level: ConfinementLevel
                            network_policy: NetworkPolicy

                            probe: SandboxProbeResult
                            cases: tuple[SandboxConformanceCaseResult, ...]

                            required_case_ids: tuple[str, ...]
                            all_required_passed: bool

                            report_hash: Sha256
                            ```

                            ---

## 6.2 Qualification must derive from the report

                            Extend `ConfinementQualification`:

                            ```python
                            class ConfinementQualification(RuntimeModel):
                                backend_name: str
                                backend_version: str | None
                                confinement_level: ConfinementLevel

                                backend_executable_identity: str
                                backend_executable_hash: Sha256

                                confinement_code_fingerprint: Sha256
                                profile_hash: Sha256
                                platform_capability_fingerprint: Sha256

                                network_policy: NetworkPolicy
                                conformance_suite_version: str

                                conformance_report_hash: Sha256
                                required_cases_passed: tuple[str, ...]

                                qualified: bool
                                qualified_at: str
                                ```

                                The only normal constructor for `qualified=True` should be:

                                ```python
                                def issue_qualification(
                                        report: SandboxConformanceReport,
                                        current_fingerprint: QualificationFingerprint,
                                        ) -> ConfinementQualification:
                                if not report.all_required_passed:
raise SandboxNotQualifiedError(...)
    ...
    ```

    Tests may still construct invalid records for negative validation cases, but production code must not accept caller-provided test names as evidence that the tests ran.

    ---

## 6.3 Explicitly require profile capabilities

    The real-engine gate should verify the full probe and profile, not merely compare an opaque platform hash.

    For the selected profile, require:

    ```text
    probe.status = USABLE

    user namespace available
    mount namespace available
    PID namespace available

    network namespace available
    when policy = DENY

    backend confinement level matches qualification

    Bubblewrap executable identity/hash match

    confinement-code fingerprint matches

    profile hash matches

    platform capability fingerprint matches

    suite version matches

    report hash resolves to an actual report

    all required cases passed
    ```

    The current gate compares fingerprints and declared test names, but a test can construct a qualification object directly. That is suitable for model validation, not as the sole attestation mechanism.

    ---

## 6.4 Qualification storage

    A qualification is host-specific. Do not use a qualification produced on a GitHub runner as authority for a user’s WSL2 machine.

    Store local qualifications under a machine-local path such as:

    ```text
    ~/.config/vibereview/qualifications/
    └── <qualification-fingerprint>.json
    ```

    or a configurable equivalent.

    The fingerprint should include:

    ```text
    Bubblewrap executable hash
    confinement code fingerprint
    sandbox profile hash
    kernel/platform capability fingerprint
    network policy
    suite version
    ```

    Any change invalidates the prior qualification automatically.

    ---

## 6.5 Add an administrative sandbox command

    This is not the full user-facing review CLI. It is a bounded runtime diagnostic entry point.

    ```bash
    python -m vibereview.runtime.sandbox probe

    python -m vibereview.runtime.sandbox qualify \
        --network-policy deny

        python -m vibereview.runtime.sandbox qualify \
            --network-policy host

            python -m vibereview.runtime.sandbox status
            ```

            Machine-readable mode:

            ```bash
            python -m vibereview.runtime.sandbox probe --json
            ```

            Exit codes:

            ```text
            0  usable / qualification passed
            2  unavailable
            3  capability blocked
            4  conformance failed
            5  internal runtime failure
            ```

            ---

## 6.6 CI workflow

### Standard deterministic matrix

            ```yaml
            pytest:
strategy:
matrix:
python-version: ["3.11", "3.12", "3.13"]

steps:
- run: python -m pytest \
        -m "not external_engine and not requires_bwrap"
        ```

        This matrix should include negative tests proving that an unusable sandbox cannot qualify.

### Qualification job

        On a capable host:

        ```yaml
        sandbox-qualification:
        runs-on: [self-hosted, linux, vibereview-sandbox]

        steps:
        - run: python -m pip install -e '.[test]'
        - run: python -m vibereview.runtime.sandbox probe --json
        - run: python -m pytest \
            -m "requires_bwrap or sandbox_conformance"
            - run: python -m vibereview.runtime.sandbox qualify \
                --network-policy deny
                ```

                Upload:

                ```text
                sandbox-probe.json
                sandbox-conformance-report.json
                confinement-qualification.json
                ```

                as CI artifacts.

                A capability-blocked hosted runner may have a separate job that proves fail-closed behavior, but that job must not be named or reported as successful qualification.

                ---

## 6.7 Branch protection

                The current `master` branch is unprotected and has no required checks.

                After the repaired workflow is green, require:

    ```text
    pytest (3.11)
    pytest (3.12)
pytest (3.13)
    sandbox-qualification
    ```

    for changes affecting:

    ```text
    runtime/confinement.py
    runtime/execution.py
    runtime/credentials.py
    runtime/trusted_launcher.py
    runtime/output_policy.py
    runtime/resource_limits.py
    engines/
    ```

    A ruleset may permit documentation-only changes to bypass the sandbox job, but code touching real-engine security boundaries must not merge without it.

    ---

# 7. Commit r5h — Receipt correctness and documentation audit

    This work is not the cause of the current CI failure, but it should be completed before a live model can create expensive or consequential results.

## 7.1 Correct receipt fallback lookup

    The current kernel appears to do the following when no exact semantic-task-key receipt exists:

    ```text
    scan previous receipts
    filter by same task type and engine
    look for transition disagreement
    ```

    This may associate an unrelated invocation of the same task type with the new task. That is an inference from the current lookup code and should be tested directly.

    Add two keys:

    ```python
    class AppliedTaskReceipt(RuntimeModel):
        input_identity_key: Sha256
        semantic_task_key: Sha256
        ...
        ```

### `input_identity_key`

        Include:

        ```text
        task type
        TaskSpec version
        prompt hash
        input schema hash
        proposal schema hash
        dependency hashes
        resource hashes
        engine-input hash
        engine identity/version/configuration
        scientific contract version
        ```

        Exclude:

        ```text
        validator fingerprint
        promotion fingerprint
        disposition fingerprint
        generation number
        ```

### `semantic_task_key`

        Include:

        ```text
        input_identity_key
        +
        validator fingerprint
        +
        promotion-handler fingerprint
        +
        disposition-handler fingerprint
        +
        runtime contract version
        ```

        Lookup behavior:

        ```text
        exact semantic key exists
        → normal receipt reuse evaluation

        exact semantic key absent
        but same input identity exists
        → semantics changed
        → reevaluation-required handling

        no input-identity match
        → unrelated task
        → do not inspect its transition
        → proceed normally
        ```

        Never fall back to matching merely by task type and engine.

        ---

## 7.2 Verify receipt payload integrity

        Before reuse:

    ```python
hash_json(receipt.proposal_payload)
    == receipt.proposal_hash
    ```

    A mismatch rejects the receipt.

    Also verify:

    ```text
    local_ref_map IDs appear among canonical object receipts
    canonical object hashes match
    task type matches
    engine identity matches
    input identity matches
    semantic fingerprint matches
    ```

    ---

## 7.3 Clarify generation semantics

    When a receipt committed in generation 5 is reused while the current project generation is 12:

    ```python
    RuntimeResult.generation = 12
    RuntimeResult.reused_generation = 5
    RuntimeResult.receipt_reused = True
    RuntimeResult.commit_performed = False
    ```

    The result operates against the current canonical view, even though the accepted effect originated in generation 5.

    ---

## 7.4 Receipt tests

    ```text
    unrelated ASSESS_CLAIM receipt
    + new ASSESS_CLAIM invocation
    → no false reevaluation error
    ```

    ```text
    same input
    + disposition handler changed
    → reevaluation required
    ```

    ```text
    same task type and engine
    + different dependencies
    → no receipt match
    ```

    ```text
    proposal payload hash mismatch
    → receipt rejected
    ```

    ```text
    receipt from generation 5 reused under generation 12
    → generation = 12
    → reused_generation = 5
    ```

    ```text
    exact replay
    → no engine call
    → no IDs
    → no generation
    ```

    ---

## 7.5 Correct documentation now

    Until sandbox qualification passes, change the README table from:

    ```text
    Qualified Linux confinement: complete
    Pre-real-engine hardening: complete
    ```

    to:

    ```text
    Bubblewrap backend implementation: complete
    Sandbox conformance qualification: failing/pending
    Final pre-real-engine gate: blocked
    ```

    The current README and handoff state that the B3 boundary is complete and all 706 tests pass, which conflicts with the failed sandbox job.

    After repair, document the actual qualification environment and results. Do not restore “complete” until a generated qualification report exists.

    ---

# 8. Final pre-Codex acceptance gate

    CodexEngine remains blocked until every item below is satisfied.

## Deterministic runtime

    ```text
    Python 3.11 deterministic tests         PASS
    Python 3.12 deterministic tests         PASS
    Python 3.13 deterministic tests         PASS
    ```

## Sandbox capability

    ```text
    Bubblewrap executable probe             PASS
    User/mount/PID namespace probe          PASS
    Selected network-policy probe           PASS
    Complete VibeReview profile probe       PASS
    ```

## Sandbox conformance

    ```text
    Host canary read denied                 PASS
    Host canary write denied                PASS
    Project root inaccessible               PASS
    Task private/ inaccessible              PASS
    Bundle immutable                        PASS
    Output and scratch writable             PASS
    DENY network test                       PASS
    HOST loopback test                      PASS
    Credential readable only as intended    PASS
    Credential diagnostic redaction         PASS
    Descendants terminated                  PASS
    ```

## Qualification evidence

    ```text
    Conformance report generated            PASS
    Qualification issued from report        PASS
    Executable hash bound                   PASS
    Code fingerprint bound                  PASS
    Profile hash bound                      PASS
    Platform capability bound               PASS
    Network policy bound                    PASS
    Real-engine gate accepts exact record   PASS
    Real-engine gate rejects mismatch       PASS
    ```

## Receipt correctness

    ```text
    Exact replay is idempotent               PASS
    Unrelated task cannot match receipt      PASS
    Handler change triggers reevaluation     PASS
    Corrupted receipt rejected               PASS
    ```

## Governance

    ```text
    Documentation matches CI                 PASS
    Required status checks configured        PASS
    No real engine implemented yet           PASS
    ```

    ---

# 9. Milestone r6a — CodexEngine adapter

    Only after the preceding gate passes should `CodexEngine` be implemented.

## 9.1 Scope

    Implement:

    ```text
    CodexEngine
    +
    ASSESS_CLAIM only
    ```

    Do not enable:

    ```text
    Deep Research parsing
    candidate generation
    evidence assessment
    proposition generation
    prose rendering
    ```

    in the first live-engine milestone.

    ---

## 9.2 Adapter responsibilities

    The adapter should perform only:

    ```text
    executable discovery
    version probing
    non-interactive mode probing
    command construction
    credential lease selection
    qualified confinement lookup
    subprocess execution
    proposal.json import
    AgentResult conversion
    ```

    Scientific logic remains in:

    ```text
    TaskSpec
    prompt
    proposal model
    promotion handler
    disposition handler
    repository validators
    ```

    ---

## 9.3 Real-engine preflight

    Before any Codex process starts:

    ```text
    Codex executable found

    version identified

    required non-interactive mode supported

    qualified HOST-network sandbox exists

    qualification fingerprint matches current host

    credential provider available

    output/proposal.json contract supported

    task type = ASSESS_CLAIM

    TaskSpec executable

    scientific inputs complete
    ```

    Failure must occur before execution and must not invoke fallback.

    Do not hard-code historical CLI flags. The adapter should probe the installed executable’s actual help/version output and fail closed when the required mode is unavailable.

    ---

## 9.4 Network profile

    A remote Codex client will require network access, so the live profile will normally use:

    ```text
    NetworkPolicy.HOST
    ```

    The qualification must accurately record:

    ```text
    filesystem and process confinement enforced
    general network egress not restricted
    ```

    Do not reuse a DENY-profile qualification for a HOST-profile execution. Their profile hashes and qualification records must differ.

    ---

## 9.5 Four qualification cases

### Case 1 — RETAIN

    ```text
    Several consistent publication-level evidence units
    → RETAIN
    ```

### Case 2 — NARROW

    ```text
    Evidence supports the proposition only under a
    defined process/material condition
    → NARROW
    ```

### Case 3 — insufficient evidence

    ```text
    Weak or sparse evidence
    → REJECT
    → insufficient_evidence
    ```

### Case 4 — contradicted

    ```text
    Substantial contrary publication-level evidence
    → REJECT
    → contradicted
    ```

    Assertions concern contracts and state:

    ```text
    proposal is valid ClaimAssessmentProposal

    ClaimAssessment canonicalized

    REJECT remains canonical

    REJECT creates no ClaimPacket

    REJECT invokes no fallback

    bundle unchanged

    credential lease cleaned

    qualification fingerprint recorded

    receipt replay invokes no second Codex call
    ```

    Do not assert exact prose.

    All live cases remain:

    ```python
    @pytest.mark.external_engine
    ```

    and must not run in ordinary pull-request CI.

    ---

# 10. Follow-up scientific roadmap

    After one Codex `ASSESS_CLAIM` path passes:

## Milestone D — Project CLI and immutable resources

    ```text
    vibereview init
    vibereview status
    vibereview validate
    vibereview run
    ```

    Add the content-addressed resource store for:

    ```text
    Deep Research Markdown
    paper Markdown
    ```

    ---

## Milestone E — Complete discovery ingestion

    Retain:

    ```text
    themes
    candidate claims
    terminology
    paper candidates
    controversies
    gaps
    source-resource provenance
    ```

    Deep Research remains discovery, not evidence.

    ---

## Milestone F — Paper concept sketches and corpus challenger

    ```text
    paper Markdown
    ↓
    paper concept sketches
    ↓
    corpus challenger
    ↓
    themes and claims omitted from Deep Research
    ↓
    merge/deduplicate
    ```

    The corpus challenger remains mandatory under the accepted scientific plan.

    ---

## Milestone G — Graphify and retrieval state

    ```text
    Graphify proposal
    ↓
    runtime retrieval ledger
    ↓
    locator/text/hash validation
    ├── invalid → ledger only, no R ID
    └── valid   → RetrievedSpan
    ↓
    exactly one RetrievalDisposition
    ↓
    assessed only
    ↓
    EvidenceRecord
    ```

    ---

## Milestone H — Evidence and claims

    ```text
    EvidenceRecord
    ↓
    ClaimPaperEvidence
    ↓
    ClaimAssessment
    ↓
    claim revision
    ↓
    FinalClaimValidation
    ↓
    Python-created ClaimPacket
    ```

    ---

## Milestone I — Writing and auditing

    ```text
    ClaimPacket
    ↓
    PropositionRecord
    ↓
    SemanticAuditResult
    ↓
    fixed placement
    ↓
    RenderedSentence
    ↓
    RenderedSentenceAudit
    ↓
    NO MORE LLM
    ↓
    deterministic citation/manuscript assembly
    ```

    ---

## Milestone J — Five-paper vertical slice

    The controlled fixture should contain:

    ```text
    one retained claim
    one narrowed claim
    one insufficient-evidence rejection
    one contradicted rejection
    one mixed-evidence publication
    one theme omitted by Deep Research but recovered by the corpus challenger
    one invalid Graphify proposal
    ```

    ---

# 11. Copy-paste Codex repair assignment

    > Implement **VibeReview r5e–r5h: sandbox qualification repair and final pre-Codex gate**, beginning from current head `3e157d503f94c301c7d0143f69018566245f86c7`.
    >
    > The frozen scientific architecture, task-resource boundary, fake subprocess boundary, credential lease, trusted launcher and accepted-task receipt architecture must not be redesigned.
    >
    > Do not implement CodexEngine, KimiEngine, AgyEngine, OpenCodeEngine, Deep Research processing, paper ingestion, Graphify, the review CLI or manuscript generation.
    >
    > The current deterministic Python 3.11–3.13 matrix passes, but the Bubblewrap sandbox-conformance job fails seven execution-based tests. Treat sandbox qualification as failed until an actual passing conformance report is generated.
    >
    > ## 1. Sandbox probe and diagnostics
    >
    > Add `SandboxProbeStatus`, `SandboxFailureCode`, `SandboxProbeCommandResult` and `SandboxProbeResult`.
    >
    > Probe:
    >
    > * Bubblewrap executable/version;
    > * minimal user/mount namespace;
    > * PID namespace;
    > * network namespace for DENY;
    > * complete VibeReview sandbox profile.
    >
    > Preserve bounded argv, exit code, stdout and stderr for every probe command.
    >
    > Distinguish:
    >
    > * executable unavailable;
    > * executable present but namespace use blocked;
    > * complete profile usable.
    >
    > Binary presence alone must not satisfy sandbox preflight.
    >
    > Modify conformance assertions so failures report:
    >
    > * sandbox probe;
    > * process argv;
    > * exit code;
    > * retained stderr;
    > * detected failures;
    > * applied limits;
    > * quiescence result.
    >
    > ## 2. Bubblewrap profile correction
    >
    > Replace opaque `--unshare-all` use with an explicit fingerprinted namespace profile.
    >
    > Common isolation should include user, PID, IPC and UTS namespaces, die-with-parent and new-session semantics. Add network namespace isolation only for `NetworkPolicy.DENY`.
    >
    > Clear and reconstruct the sandbox environment. Inside the sandbox set:
    >
    > * `HOME=/work/home`;
    > * `XDG_CONFIG_HOME=/work/home/.config`;
    > * `XDG_CACHE_HOME=/work/home/.cache`;
    > * `TMPDIR=/work/tmp`;
    > * controlled PATH/locale values.
    >
    > Preserve the existing mount boundary:
    >
    > * bundle and launcher read-only;
    > * output, scratch, home and tmp writable;
    > * credentials minimally exposed;
    > * project root and task private state unavailable.
    >
    > Add a genuine HOST-network execution test using a temporary loopback server and a DENY-network test using the same endpoint.
    >
    > ## 3. Capability-aware CI
    >
    > Ordinary CI must test that blocked or unavailable sandbox environments fail closed.
    >
    > Positive sandbox qualification must run only after the complete Bubblewrap profile probe reports usable.
    >
    > Prefer a dedicated capable Linux runner. A hosted Ubuntu runner may be prepared with an application-specific Bubblewrap AppArmor profile, but do not globally disable AppArmor user-namespace restrictions merely to make the job pass.
    >
    > Upload the probe and conformance report as workflow artifacts.
    >
    > ## 4. Attested qualification
    >
    > Add per-case `SandboxConformanceCaseResult` and an aggregate `SandboxConformanceReport`.
    >
    > Generate `ConfinementQualification(qualified=True)` only from a report in which every required case passed.
    >
    > Bind qualification to:
    >
    > * actual Bubblewrap path and hash;
    > * confinement implementation fingerprint;
    > * explicit sandbox profile hash;
    > * full platform capability fingerprint;
    > * network policy;
    > * conformance-suite version;
    > * conformance-report hash.
    >
    > Update `require_real_engine_qualification()` to require the actual probe capabilities needed by the selected profile, not merely a matching opaque fingerprint and caller-supplied test names.
    >
    > Add a machine-local sandbox probe/qualification command under `python -m vibereview.runtime.sandbox`.
    >
    > ## 5. Accepted-task receipt correction
    >
    > Add an `input_identity_key` separate from the fingerprinted `semantic_task_key`.
    >
    > Do not scan receipts merely by task type and engine when an exact key is absent.
    >
    > A semantics-change reevaluation may consider only a receipt with the same complete input identity.
    >
    > Recompute and verify `proposal_hash` from `proposal_payload`.
    >
    > On receipt reuse:
    >
    > * report the current canonical generation in `RuntimeResult.generation`;
    > * report the historical committed generation in `reused_generation`;
    > * call no engine;
    > * allocate no IDs;
    > * create no generation.
    >
    > Add tests proving that unrelated invocations of the same TaskType and engine cannot trigger a false reevaluation error.
    >
    > ## 6. Documentation and repository governance
    >
    > Until the sandbox suite is green, document:
    >
    > * Bubblewrap backend implemented;
    > * qualification failed/pending;
    > * final pre-Codex gate blocked.
    >
    > Remove claims that 706/706 tests passed.
    >
    > After repair, update documentation only with actual workflow evidence.
    >
    > Add required branch checks for the Python 3.11–3.13 matrix and the qualified sandbox job before live-engine work is merged.
    >
    > ## Required test gates
    >
    > The milestone is complete only when:
    >
    > * deterministic Python 3.11 tests pass;
    > * deterministic Python 3.12 tests pass;
    > * deterministic Python 3.13 tests pass;
    > * complete Bubblewrap profile probe passes on the qualification host;
    > * every required sandbox conformance case passes;
    > * a qualification artifact is generated from the passing report;
    > * the real-engine gate accepts the exact current qualification;
    > * the gate rejects executable, code, profile, platform, network-policy and report mismatches;
    > * receipt replay remains idempotent;
    > * unrelated receipts cannot match;
    > * documentation agrees with CI.
    >
    > Stop before implementing any real LLM engine.
    >
    > Return:
    >
    > * files changed;
    > * exact sandbox probe results;
    > * original sandbox failure cause;
    > * corrected Bubblewrap argv/profile;
    > * CI-host preparation;
    > * conformance case results;
    > * generated qualification fingerprint;
    > * receipt lookup changes;
    > * complete local pytest results;
    > * GitHub Actions results;
    > * confirmation that no real LLM engine was implemented.

    The first action in this plan is to expose the actual Bubblewrap stderr. No AppArmor, namespace, mount or command-line correction should be treated as confirmed until that evidence has been captured.

    [1]: https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/?utm_source=chatgpt.com "AppArmor - Ubuntu security documentation"
    [2]: https://documentation.ubuntu.com/release-notes/24.04/?utm_source=chatgpt.com "Ubuntu 24.04 LTS release notes - Ubuntu release notes"

