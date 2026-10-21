"""LLM API keys in the OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service).

Keys are stored with the `keyring` package under the service "better-captions", one entry per
provider. Older installs kept them in plain text in ``<data>/config.json``; those are still read
(with a warning) and ``better-captions key migrate`` moves them into the keychain.

Without a usable keychain (headless Linux has no Secret Service) every keychain call raises
KeychainUnavailable with a message pointing at the environment variables instead.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from better_captions import paths

log = logging.getLogger(__name__)

SERVICE = "better-captions"
CONFIG_FILE = "config.json"
# provider -> (environment variable, legacy config.json key)
PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "openai": ("OPENAI_API_KEY", "openai_api_key"),
}


class KeychainUnavailable(RuntimeError):
    """No working OS keychain backend on this machine."""


def env_var(provider: str) -> str:
    return PROVIDERS[_check(provider)][0]


def _check(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
    return provider


def _unavailable(reason: str) -> KeychainUnavailable:
    names = " / ".join(var for var, _ in PROVIDERS.values())
    return KeychainUnavailable(f"OS keychain unavailable ({reason}); set {names} in the environment instead")


def _backend():
    try:
        import keyring
    except ImportError as exc:
        raise _unavailable(f"keyring package missing: {exc}") from exc
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc
    # keyring's fail/null backends (no Secret Service, no D-Bus session) have priority <= 0.
    if getattr(backend, "priority", 1) <= 0:
        raise _unavailable(f"no usable backend, keyring chose {type(backend).__module__}.{type(backend).__name__}")
    return backend


def backend_name() -> str:
    """The keychain backend in use. Raises KeychainUnavailable."""
    backend = _backend()
    return f"{type(backend).__module__}.{type(backend).__name__}"


def get_key(provider: str) -> str | None:
    """The provider's key from the keychain, or None. Raises KeychainUnavailable."""
    _check(provider)
    backend = _backend()
    try:
        return backend.get_password(SERVICE, provider) or None
    except Exception as exc:  # locked keychain, D-Bus errors, ...
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc


def set_key(provider: str, key: str) -> None:
    _check(provider)
    key = key.strip()
    if not key:
        raise ValueError("empty API key")
    backend = _backend()
    try:
        backend.set_password(SERVICE, provider, key)
    except Exception as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc


def delete_key(provider: str) -> bool:
    """Remove the provider's key. False if there was none. Raises KeychainUnavailable."""
    if get_key(provider) is None:
        return False
    try:
        _backend().delete_password(SERVICE, provider)
    except Exception as exc:
        raise _unavailable(f"{type(exc).__name__}: {exc}") from exc
    return True


# --- legacy config.json -----------------------------------------------------------------------


def config_path() -> Path:
    return paths.data_dir() / CONFIG_FILE


def _read_config(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def legacy_key(provider: str) -> str | None:
    """A plain-text key left in config.json by an older version, or None."""
    value = _read_config(config_path()).get(PROVIDERS[_check(provider)][1])
    return (value.strip() or None) if isinstance(value, str) else None


def remove_legacy_keys(providers) -> None:
    """Drop the given providers' keys from config.json, keeping every other setting."""
    path = config_path()
    config = _read_config(path)
    fields = [PROVIDERS[_check(p)][1] for p in providers]
    if not any(f in config for f in fields):
        return
    for f in fields:
        config.pop(f, None)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def migrate() -> dict[str, str]:
    """Move keys from config.json into the keychain: {provider: "migrated" | "kept-existing"}.

    A key already in the keychain is not overwritten; the plain-text copy is removed either way.
    Raises KeychainUnavailable before touching the file if there is no keychain.
    """
    found = {p: k for p in PROVIDERS if (k := legacy_key(p))}
    if not found:
        return {}
    _backend()
    done: dict[str, str] = {}
    for provider, key in found.items():
        if get_key(provider):
            done[provider] = "kept-existing"
            continue
        set_key(provider, key)
        if get_key(provider) != key:
            raise _unavailable(f"the {provider} key did not read back from the keychain")
        done[provider] = "migrated"
    remove_legacy_keys(done)
    return done


# --- lookup -----------------------------------------------------------------------------------


def lookup(provider: str) -> str | None:
    """Env var > keychain > legacy config.json (with a migration warning). Explicit options come first in the caller."""
    var, field = PROVIDERS[_check(provider)]
    key = os.environ.get(var)
    if key:
        return key
    try:
        key = get_key(provider)
    except KeychainUnavailable as exc:
        log.warning("%s", exc)
        key = None
    if key:
        return key
    key = legacy_key(provider)
    if key:
        log.warning(
            "using the %s API key stored in plain text in %s (%r); run `better-captions key migrate` "
            "to move it into the OS keychain",
            provider,
            config_path(),
            field,
        )
    return key


def status(provider: str) -> dict:
    """Where the provider's key is set, without the key itself."""
    var, _ = PROVIDERS[_check(provider)]
    try:
        keychain: bool | None = get_key(provider) is not None
    except KeychainUnavailable:
        keychain = None
    return {"env": bool(os.environ.get(var)), "keychain": keychain, "config": legacy_key(provider) is not None}
