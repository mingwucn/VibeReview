"""Fixed-file credential and trusted in-sandbox shim tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vibereview.runtime import credential_exec_shim, credentials as credentials_module
from vibereview.runtime.credentials import (
    CredentialStagingPaths,
    EnvironmentFileCredentialProvider,
    FixedFileCredentialLease,
    SyntheticCredentialProvider,
)


def test_environment_secret_is_loaded_only_inside_lease_and_never_becomes_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "deepseek-secret-token-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")
    provider = EnvironmentFileCredentialProvider(
        env_var="DEEPSEEK_API_KEY", provider_id="deepseek-rest"
    )
    lease = provider.prepare_for_session("deepseek-rest", paths)
    staged = paths.credentials_dir / "credential"
    assert not staged.exists()

    with lease:
        assert staged.read_bytes() == secret.encode()
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        assert lease.context.secret_env == {}
        assert lease.context.fixed_file_name == "credential"
        assert secret not in repr(lease.context)
        assert secret not in str(lease.context.model_dump(mode="json"))
        assert secret.encode() in lease.context.exact_redaction_values
    assert not staged.exists()


def test_missing_environment_secret_fails_on_enter_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIBEREVIEW_TEST_MISSING_KEY", raising=False)
    provider = EnvironmentFileCredentialProvider(
        env_var="VIBEREVIEW_TEST_MISSING_KEY", provider_id="missing"
    )
    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")
    lease = provider.prepare_for_session("missing", paths)
    with pytest.raises(RuntimeError, match="required credential"):
        with lease:
            pass
    assert not (paths.credentials_dir / "credential").exists()


def test_short_environment_secret_remains_exact_redaction_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBEREVIEW_SHORT_SECRET", "x")
    provider = EnvironmentFileCredentialProvider(
        env_var="VIBEREVIEW_SHORT_SECRET", provider_id="short-secret-test"
    )
    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")

    with provider.prepare_for_session("short-secret-test", paths) as lease:
        assert lease.context.exact_redaction_values == (b"x",)

    assert not (paths.credentials_dir / "credential").exists()


def test_credential_shim_stages_only_fixed_file_and_execs_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "credentials"
    attempt_home = tmp_path / "attempt-home"
    normal_home = tmp_path / "normal-home"
    credentials.mkdir()
    attempt_home.mkdir()
    normal_home.mkdir()
    secret = b"oauth-secret-token-value"
    (credentials / "credential").write_bytes(secret)
    (normal_home / "host-profile.json").write_text("DO_NOT_COPY", encoding="utf-8")
    monkeypatch.setattr(credential_exec_shim, "_CREDENTIAL_ROOT", credentials)
    monkeypatch.setattr(credential_exec_shim, "_HOME_ROOT", attempt_home)
    monkeypatch.setenv("HOME", str(normal_home))

    captured: dict[str, object] = {}

    class ExecCaptured(BaseException):
        pass

    def fake_exec(file, argv, environment):
        captured.update(file=file, argv=argv, environment=environment)
        raise ExecCaptured

    monkeypatch.setattr(os, "execvpe", fake_exec)
    with pytest.raises(ExecCaptured):
        credential_exec_shim.main(
            [
                "--credential-name", "credential",
                "--home-target", ".vendor/oauth.json",
                "--", "/work/launcher/worker", "--safe-flag",
            ]
        )
    target = attempt_home / ".vendor/oauth.json"
    assert target.read_bytes() == secret
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert captured["argv"] == ["/work/launcher/worker", "--safe-flag"]
    assert secret.decode() not in repr(captured)
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["VIBEREVIEW_FIXED_CREDENTIAL"].endswith("/credential")
    assert environment["HOME"] == str(attempt_home)
    assert (normal_home / "host-profile.json").read_text(encoding="utf-8") == "DO_NOT_COPY"
    assert not (attempt_home / "host-profile.json").exists()


@pytest.mark.parametrize("name", ["CODEX_API_KEY", "KIMI_MODEL_API_KEY"])
def test_credential_shim_sets_only_allowlisted_immediate_cli_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    credentials = tmp_path / "credentials"
    home = tmp_path / "home"
    credentials.mkdir()
    home.mkdir()
    secret = "fixed-cli-secret"
    (credentials / "credential").write_text(secret, encoding="utf-8")
    monkeypatch.setattr(credential_exec_shim, "_CREDENTIAL_ROOT", credentials)
    monkeypatch.setattr(credential_exec_shim, "_HOME_ROOT", home)
    captured: dict[str, object] = {}

    class Captured(BaseException):
        pass

    def fake_exec(file, argv, environment):
        captured.update(file=file, argv=argv, environment=environment)
        raise Captured

    monkeypatch.setattr(os, "execvpe", fake_exec)
    with pytest.raises(Captured):
        credential_exec_shim.main(
            ["--credential-name", "credential", "--credential-env", name, "--", "cli"]
        )
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment[name] == secret
    assert not ({"CODEX_API_KEY", "KIMI_MODEL_API_KEY"} - {name}) & set(environment)
    assert secret not in captured["argv"]


@pytest.mark.parametrize(
    "name", ["../escape", "-option", "bad\x00name", "bad\nname", "a" * 129]
)
def test_synthetic_provider_rejects_unsafe_fixture_filename(
    tmp_path: Path, name: str
) -> None:
    provider = SyntheticCredentialProvider(file_credentials={name: "secret"})
    with pytest.raises(ValueError, match="unsafe fixed credential"):
        provider.prepare("test", tmp_path / "credentials")


@pytest.mark.parametrize("secret", ["line\n", "line\r"])
def test_environment_provider_rejects_non_scalar_secret_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    monkeypatch.setenv("VIBEREVIEW_SCALAR_TEST", secret)
    provider = EnvironmentFileCredentialProvider(
        env_var="VIBEREVIEW_SCALAR_TEST", provider_id="scalar-test"
    )
    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")
    lease = provider.prepare_for_session("scalar-test", paths)
    with pytest.raises(RuntimeError, match="one exact scalar"):
        with lease:
            pass
    assert not (paths.credentials_dir / "credential").exists()


def test_lease_activation_failure_after_write_cleans_file_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Lock:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *args):
            events.append("exit")

    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")
    lease = FixedFileCredentialLease(
        provider_id="test", paths=paths, fixed_file_name="credential",
        secret_loader=lambda: b"secret-token", max_bytes=100, lock=Lock(),
    )
    monkeypatch.setattr(
        credentials_module, "_nonsecret_fingerprint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected context failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected context failure"):
        with lease:
            pass
    assert not (paths.credentials_dir / "credential").exists()
    assert events == ["enter", "exit"]
    assert lease._entered is False


def test_fixed_lease_releases_lock_and_resets_after_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Lock:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *args):
            events.append("exit")

    paths = CredentialStagingPaths(tmp_path / "credentials", tmp_path / "home")
    lease = FixedFileCredentialLease(
        provider_id="test",
        paths=paths,
        fixed_file_name="credential",
        secret_loader=lambda: b"secret-token",
        max_bytes=100,
        lock=Lock(),
    )
    real_unlink = Path.unlink

    def fail_staged(self, *args, **kwargs):
        if self.name == "credential":
            raise OSError("injected unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_staged)
    with pytest.raises(OSError, match="injected unlink failure"):
        with lease:
            pass
    assert events == ["enter", "exit"]
    assert lease._entered is False


@pytest.mark.parametrize("target", ["../escape.json", "/absolute.json"])
def test_credential_shim_rejects_unsafe_home_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    credentials = tmp_path / "credentials"
    home = tmp_path / "home"
    credentials.mkdir()
    home.mkdir()
    (credentials / "credential").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(credential_exec_shim, "_CREDENTIAL_ROOT", credentials)
    monkeypatch.setattr(credential_exec_shim, "_HOME_ROOT", home)
    assert credential_exec_shim.main(
        ["--credential-name", "credential", "--home-target", target, "--", "true"]
    ) == 125
    assert list(home.rglob("*")) == []


@pytest.mark.parametrize("name", ["-option", "bad\x00name", "bad\nname"])
def test_credential_shim_rejects_unsafe_fixed_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    del tmp_path, monkeypatch
    with pytest.raises(ValueError, match="unsafe credential filename"):
        credential_exec_shim._safe_name(name)


@pytest.mark.parametrize("name", ["lowercase", "BAD-NAME", "A" * 129])
def test_environment_provider_rejects_unsafe_source_env_name(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe credential environment"):
        EnvironmentFileCredentialProvider(env_var=name, provider_id="test")
