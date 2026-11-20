"""The encrypted secret store (agentkit/secrets)."""
import os
import stat

import pytest

from agentkit.secrets import SecretStore, SecretStoreError


def test_round_trip_is_encrypted_and_private(tmp_path):
    store = SecretStore(tmp_path / "s")
    store.put("google", {"refresh_token": "1//abc-very-secret"}, kind="oauth", meta={"account": "me"})
    assert store.get("google") == {"refresh_token": "1//abc-very-secret"}
    raw = (tmp_path / "s" / "secrets.enc").read_bytes()
    assert b"very-secret" not in raw and b"refresh_token" not in raw
    for name in ("secrets.enc", "master.key"):
        assert stat.S_IMODE(os.stat(tmp_path / "s" / name).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "s").st_mode) == 0o700
    listing = store.names()
    assert listing[0]["name"] == "google" and "value" not in listing[0] and listing[0]["meta"] == {"account": "me"}
    assert "very-secret" not in repr(store) + str(listing)


def test_a_new_instance_reads_with_the_same_key_and_delete_works(tmp_path):
    SecretStore(tmp_path).put("site:srt", {"cookies": [1, 2]}, kind="browser_state")
    again = SecretStore(tmp_path)
    assert again.get("site:srt") == {"cookies": [1, 2]}
    assert again.delete("site:srt") and again.get("site:srt") is None and not again.delete("site:srt")


def test_another_key_cannot_read_it(tmp_path):
    SecretStore(tmp_path).put("x", "v")
    (tmp_path / "master.key").write_bytes(b"Yk9mX0xYbV9oV2VfZnJlc2hfa2V5X2Zvcl90ZXN0cyE=\n")
    os.chmod(tmp_path / "master.key", 0o600)
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path).get("x")


def test_a_key_readable_by_others_is_refused(tmp_path):
    SecretStore(tmp_path).put("x", "v")
    os.chmod(tmp_path / "master.key", 0o644)
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path).get("x")
