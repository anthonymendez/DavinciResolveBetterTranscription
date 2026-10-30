"""Shared fixtures. No test touches the real OS keychain (an in-memory backend is installed for all)
or a real coding-agent CLI.

tests/resolve also runs on a bare Python without the engine's dependencies (CI checks the plugin on
every Python Resolve may use); keyring is missing there, so these fixtures do nothing. With
keyring installed they run in full, so a broken engine import still fails loudly."""

import pytest

try:
    import keyring
    from keyring.backend import KeyringBackend
except ImportError:  # bare-Python plugin run: no engine dependencies installed
    keyring = None
    KeyringBackend = object


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if self.store.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def no_agent_clis(monkeypatch):
    """ "auto" naming must never find (and run) a real claude / agy installed on this machine."""
    if keyring is None:
        return
    from better_captions.naming import agent_cli

    monkeypatch.setattr(agent_cli, "available", lambda cli, options: None)


@pytest.fixture(autouse=True)
def fake_keyring():
    if keyring is None:
        yield None
        return
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)
