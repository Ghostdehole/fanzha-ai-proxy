from dataclasses import replace

import pytest

from client import AntiFraudClient
from config import AppConfig, validate_chat_key


def test_optional_chat_key_reads_environment_and_is_omitted_from_repr(monkeypatch):
    monkeypatch.delenv("FANZHA_CHAT_KEY", raising=False)
    assert AppConfig().chat_key == ""
    monkeypatch.setenv("FANZHA_CHAT_KEY", "example-chat-key")
    cfg = AppConfig()
    assert cfg.chat_key == "example-chat-key"
    assert "example-chat-key" not in repr(cfg)
    assert "chat_key" not in repr(cfg)
    assert replace(cfg, chat_key="").chat_key == ""


@pytest.mark.parametrize("value", [
    "example\r\nInjected: true", "example\n", "example\r", "example\tkey",
    "example\x00key", "example\x1fkey", "example\x7fkey", "example\u0085key",
    "example\u2028key", "example\ud800key", " example", "example ",
    "example key", None, 123, b"example",
])
def test_chat_key_configuration_rejects_invalid_header_values_without_echo(value):
    with pytest.raises(ValueError) as caught:
        AppConfig(chat_key=value)
    assert str(caught.value) == "FANZHA_CHAT_KEY must be empty or contain only visible ASCII characters without whitespace"


def test_chat_key_accepts_visible_ascii_without_rewriting():
    value = "example-chat-key_./+=:~!"
    assert validate_chat_key(value) == value
    assert AppConfig(chat_key=value).chat_key == value


def test_client_validates_custom_configuration_too():
    from types import SimpleNamespace
    with pytest.raises(ValueError, match="FANZHA_CHAT_KEY"):
        AntiFraudClient(SimpleNamespace(chat_key="example\ninvalid"), None)
