"""The server secret behind chat and sign-in signatures."""

import os
import sys

import pytest

import aces_keys


def test_each_purpose_gets_its_own_key():
    assert aces_keys.derive("chat-tool-result") != aces_keys.derive("session")
    key = aces_keys.derive("chat-tool-result")
    signature = aces_keys.sign(key, b"data")
    assert aces_keys.verify(key, b"data", signature)
    assert not aces_keys.verify(aces_keys.derive("session"), b"data", signature)
    assert not aces_keys.verify(key, b"data", "")


def test_the_secret_file_is_created_once_and_reused(tmp_path):
    path = str(tmp_path / "secret.key")
    first = aces_keys._from_file(path)
    assert len(first) >= aces_keys.MIN_SECRET_CHARS
    assert aces_keys._from_file(path) == first
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o077 == 0, "only the server's own account may read it"


def test_a_short_secret_file_is_refused(tmp_path):
    path = tmp_path / "secret.key"
    path.write_text("too-short", encoding="ascii")
    with pytest.raises(ValueError):
        aces_keys._from_file(str(path))
