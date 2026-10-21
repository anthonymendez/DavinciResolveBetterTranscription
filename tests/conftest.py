"""Shared fixtures. No test touches the real OS keychain (an in-memory backend is installed for all)
or a real coding-agent CLI."""

import keyring
import pytest
from keyring.backend import KeyringBackend


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
    from better_captions.naming import agent_cli

    monkeypatch.setattr(agent_cli, "available", lambda cli, options: None)


@pytest.fixture(autouse=True)
def fake_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)
