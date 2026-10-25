"""Auxiliary recovery respects a provider's runtime-owned credential pool."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import providers
from providers.base import ProviderProfile
from agent import auxiliary_client as aux
from agent.credential_pool import CredentialPool, PooledCredential, STATUS_DEAD
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_cli.auth_constants import AuthError
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


_PROVIDER = "example-aux-pinned"
_STORAGE = "example-aux-native"


@pytest.fixture
def register_profiles():
    def register(profile):
        providers.register_provider(profile)

    yield register
    for name in (_PROVIDER, _STORAGE):
        providers._REGISTRY.pop(name, None)
        providers._SOURCES.pop(name, None)
    providers._PROVIDER_LIST_CACHE = None


class _PoolFacade:
    provider = _PROVIDER

    def __init__(self, pool):
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._pool, name)


def _auth_error():
    error = Exception("test access token expired")
    error.status_code = 401
    return error


@pytest.mark.parametrize("outcome", ["refresh", "peer-rotation", "terminal"])
def test_runtime_pool_recovery_keeps_native_storage_and_profile_scope(
    monkeypatch, tmp_path, register_profiles, outcome,
):
    """Use the plugin's native pool; an already replaced bearer needs no second refresh."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    refreshes = []

    def refresh(entry):
        refreshes.append(entry.refresh_token)
        if outcome == "terminal":
            raise AuthError("invalid_grant", provider=_STORAGE, code="invalid_grant",
                            relogin_required=True)
        return {"access_token": f"test-refreshed-access-{len(refreshes)}",
                "refresh_token": f"test-refreshed-refresh-{len(refreshes)}"}

    register_profiles(ProviderProfile(name=_STORAGE, auth_type="oauth_external",
                                      refresh_credential=refresh))

    class PinnedProfile(ProviderProfile):
        def resolve_runtime(self, *, target_model, explicit_api_key, explicit_base_url):
            assert (target_model, explicit_api_key, explicit_base_url) == (None, None, None)
            rows = [PooledCredential.from_dict(_STORAGE, row)
                    for row in read_credential_pool(_STORAGE)]
            pool = _PoolFacade(CredentialPool(_STORAGE, rows))
            entry = pool.select()
            return {"provider": _PROVIDER, "api_key": entry.runtime_api_key if entry else "",
                    "credential_pool": pool}

    register_profiles(PinnedProfile(name=_PROVIDER, auth_type="oauth_external"))
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            write_credential_pool(_STORAGE, [{
                "id": "test-account", "auth_type": "oauth", "source": "manual:test",
                "access_token": "test-peer-access" if outcome == "peer-rotation" else "test-old-access",
                "refresh_token": "test-refresh", "expires_at_ms": 4102444800000,
            }])
        finally:
            reset_hermes_home_override(token)

    monkeypatch.setattr(aux, "_client_cache", {})
    for home in (homes[0], homes[1], homes[0]):
        token = set_hermes_home_override(home)
        try:
            before = read_credential_pool(_STORAGE)[0]
            failed_key = "test-old-access" if outcome == "peer-rotation" else before["access_token"]
            key = aux._client_cache_key(_PROVIDER, async_mode=False)
            other_key = aux._client_cache_key("openrouter", async_mode=False)
            aux._client_cache[key] = (SimpleNamespace(), "test-model", None)
            aux._client_cache[other_key] = (SimpleNamespace(), "other-model", None)

            with monkeypatch.context() as recovery_patch:
                recovery_patch.setattr(aux, "load_pool", lambda provider: pytest.fail("must use the runtime-owned pool"))
                assert aux._recover_provider_pool(_PROVIDER, _auth_error(), failed_api_key=failed_key) is (
                    outcome != "terminal")

            assert (key not in aux._client_cache) is (outcome != "terminal")
            assert other_key in aux._client_cache
            assert read_credential_pool(_PROVIDER) == []
            after = read_credential_pool(_STORAGE)[0]
            if outcome == "refresh":
                assert after["access_token"] == f"test-refreshed-access-{len(refreshes)}"
            elif outcome == "peer-rotation":
                assert after["access_token"] == "test-peer-access"
                assert refreshes == []
            else:
                assert after["last_status"] == STATUS_DEAD
        finally:
            reset_hermes_home_override(token)


@pytest.mark.parametrize("outcome", ["raises", "no-pool"])
def test_owned_runtime_recovery_does_not_fall_back_to_ambient_pool(
    monkeypatch, register_profiles, outcome,
):
    class PinnedProfile(ProviderProfile):
        def resolve_runtime(self, **kwargs):
            if outcome == "raises":
                raise RuntimeError("test account unavailable")
            return {"provider": _PROVIDER, "api_key": "test-unrelated-access"}

    register_profiles(PinnedProfile(name=_PROVIDER, auth_type="oauth_external"))
    monkeypatch.setattr(aux, "load_pool", lambda provider: pytest.fail("must not use an ambient pool"))
    assert aux._recover_provider_pool(_PROVIDER, _auth_error(), failed_api_key="test-old-access") is False
