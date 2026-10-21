"""API keys in the OS keychain (in-memory fake backend from conftest), legacy config.json, `key` CLI."""

import io
import json

import keyring
import pytest
from keyring.backends import fail

from better_captions import cli, secrets


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path))


@pytest.fixture
def no_keychain():
    keyring.set_keyring(fail.Keyring())


class BrokenKeyring(keyring.backend.KeyringBackend):
    """A backend that exists but errors, like a locked keychain or a dead D-Bus session."""

    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")

    def set_password(self, service, username, password):
        raise keyring.errors.PasswordSetError("locked")

    def delete_password(self, service, username):
        raise keyring.errors.PasswordDeleteError("locked")


def write_config(tmp_path, **fields):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"engine": ["py", "-m", "better_captions.cli"], "naming_provider": "auto", **fields}),
        encoding="utf-8",
    )
    return path


def read_config(tmp_path):
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


# --- keychain -----------------------------------------------------------------------------------


def test_set_get_delete_under_service_name(fake_keyring):
    assert secrets.get_key("anthropic") is None
    secrets.set_key("anthropic", "  sk-ant\n")
    assert fake_keyring.store == {("better-captions", "anthropic"): "sk-ant"}
    assert secrets.get_key("anthropic") == "sk-ant"
    assert secrets.get_key("openai") is None
    assert secrets.delete_key("anthropic") is True
    assert secrets.delete_key("anthropic") is False
    assert fake_keyring.store == {}


def test_rejects_empty_key_and_unknown_provider():
    with pytest.raises(ValueError):
        secrets.set_key("anthropic", "  \n")
    with pytest.raises(ValueError):
        secrets.get_key("gemini")


@pytest.mark.usefixtures("no_keychain")
def test_missing_backend_raises_clear_error():
    with pytest.raises(secrets.KeychainUnavailable, match="ANTHROPIC_API_KEY / OPENAI_API_KEY"):
        secrets.get_key("anthropic")
    with pytest.raises(secrets.KeychainUnavailable):
        secrets.set_key("anthropic", "k")


def test_erroring_backend_raises_keychain_unavailable():
    keyring.set_keyring(BrokenKeyring())
    for call in (
        lambda: secrets.get_key("openai"),
        lambda: secrets.set_key("openai", "k"),
        lambda: secrets.delete_key("openai"),
    ):
        with pytest.raises(secrets.KeychainUnavailable, match="KeyringLocked|PasswordSetError"):
            call()


def test_lookup_order(monkeypatch, tmp_path, caplog):
    assert secrets.lookup("openai") is None
    write_config(tmp_path, openai_api_key="sk-config")
    assert secrets.lookup("openai") == "sk-config"
    assert "plain text" in caplog.text and "key migrate" in caplog.text
    secrets.set_key("openai", "sk-keychain")
    assert secrets.lookup("openai") == "sk-keychain"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert secrets.lookup("openai") == "sk-env"


@pytest.mark.usefixtures("no_keychain")
def test_lookup_without_keychain_uses_env_and_legacy(monkeypatch, tmp_path, caplog):
    assert secrets.lookup("anthropic") is None
    assert "OS keychain unavailable" in caplog.text
    write_config(tmp_path, anthropic_api_key="sk-config")
    assert secrets.lookup("anthropic") == "sk-config"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert secrets.lookup("anthropic") == "sk-env"


# --- migrate ------------------------------------------------------------------------------------


def test_migrate_moves_keys_and_keeps_other_settings(tmp_path, fake_keyring):
    write_config(tmp_path, anthropic_api_key="sk-ant", openai_api_key="sk-oa")
    assert secrets.migrate() == {"anthropic": "migrated", "openai": "migrated"}
    assert secrets.get_key("anthropic") == "sk-ant" and secrets.get_key("openai") == "sk-oa"
    assert read_config(tmp_path) == {"engine": ["py", "-m", "better_captions.cli"], "naming_provider": "auto"}
    assert secrets.migrate() == {}


def test_migrate_keeps_existing_keychain_key_and_reads_bom_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"engine": ["py"], "anthropic_api_key": "sk-old"}), encoding="utf-8-sig"
    )  # PowerShell BOM
    secrets.set_key("anthropic", "sk-new")
    assert secrets.migrate() == {"anthropic": "kept-existing"}
    assert secrets.get_key("anthropic") == "sk-new"
    assert read_config(tmp_path) == {"engine": ["py"]}


@pytest.mark.usefixtures("no_keychain")
def test_migrate_without_keychain_leaves_config_untouched(tmp_path):
    path = write_config(tmp_path, anthropic_api_key="sk-ant")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(secrets.KeychainUnavailable):
        secrets.migrate()
    assert path.read_text(encoding="utf-8") == before


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_set_from_stdin(monkeypatch, capsys, fake_keyring):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-stdin-secret\n"))
    assert cli.main(["key", "set", "anthropic", "--stdin"]) == 0
    assert fake_keyring.store[("better-captions", "anthropic")] == "sk-stdin-secret"
    assert "sk-stdin-secret" not in capsys.readouterr().out


def test_cli_set_prompts_with_getpass(monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "sk-typed")
    assert cli.main(["key", "set", "openai"]) == 0
    assert prompts == ["openai API key: "]
    assert secrets.get_key("openai") == "sk-typed"
    assert "sk-typed" not in capsys.readouterr().out


def test_cli_set_empty_key_fails(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["key", "set", "anthropic", "--stdin"]) == 1
    assert "empty API key" in capsys.readouterr().err


def test_cli_delete(capsys):
    secrets.set_key("anthropic", "sk-x")
    assert cli.main(["key", "delete", "anthropic"]) == 0
    assert "removed" in capsys.readouterr().out
    assert secrets.get_key("anthropic") is None
    assert cli.main(["key", "delete", "anthropic"]) == 0
    assert "no key" in capsys.readouterr().out


def test_cli_status_never_prints_keys(monkeypatch, tmp_path, capsys):
    secrets.set_key("anthropic", "sk-keychain-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret")
    write_config(tmp_path, openai_api_key="sk-config-secret")
    assert cli.main(["key", "status"]) == 0
    out = capsys.readouterr().out
    assert "secret" not in out
    lines = dict(line.split(": ", 1) for line in out.strip().splitlines())
    assert lines["anthropic"] == "keychain"
    assert lines["openai"].startswith("env OPENAI_API_KEY, config.json")
    assert "MemoryKeyring" in lines["keychain"]


@pytest.mark.usefixtures("no_keychain")
def test_cli_without_keychain_explains_env_fallback(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-x\n"))
    assert cli.main(["key", "set", "anthropic", "--stdin"]) == 1
    assert "OS keychain unavailable" in capsys.readouterr().err
    assert cli.main(["key", "status"]) == 0
    out = capsys.readouterr().out
    assert "keychain: OS keychain unavailable" in out and "ANTHROPIC_API_KEY" in out
    assert "anthropic: not set" in out


def test_cli_migrate(tmp_path, capsys):
    write_config(tmp_path, anthropic_api_key="sk-ant-secret")
    assert cli.main(["key", "migrate"]) == 0
    out = capsys.readouterr().out
    assert "anthropic: moved into the OS keychain" in out and "sk-ant-secret" not in out
    assert "anthropic_api_key" not in read_config(tmp_path)
    assert secrets.get_key("anthropic") == "sk-ant-secret"
    assert cli.main(["key", "migrate"]) == 0
    assert "no API keys" in capsys.readouterr().out
