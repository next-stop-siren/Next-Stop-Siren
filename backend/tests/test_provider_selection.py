"""Provider opt-in and worker startup without sockets, credentials or a database."""

import asyncio
from unittest.mock import Mock, patch

import pytest

from app.integrations.codyssey import APPROVED_BASE_URL, APPROVED_MODEL, CodysseyGptProvider, GptSettingsError
from app.integrations.provider import MockProvider, ProviderSettingsError, get_provider
from app.main import create_app, lifespan

SETTINGS = {
    "CHAT_PROVIDER": "codyssey",
    "CODYSSEY_API_KEY": "fixture-only-codyssey-key",
    "CODYSSEY_BASE_URL": APPROVED_BASE_URL,
    "CODYSSEY_MODEL": APPROVED_MODEL,
}


@pytest.mark.parametrize("values", [{}, {"CHAT_PROVIDER": "mock"}, {**SETTINGS, "CHAT_PROVIDER": "mock"}])
def test_mock_is_default_and_does_not_read_codyssey_settings(values):
    with patch("app.integrations.codyssey.GptSettings.from_env", side_effect=AssertionError("unused")):
        assert isinstance(get_provider(values), MockProvider)


@pytest.mark.parametrize("selected", ["", "Codyssey", "codyssey ", " mock", "other-private-selection"])
def test_unknown_or_ambiguous_selection_never_falls_back_or_echoes_input(selected):
    with pytest.raises(ProviderSettingsError) as caught:
        get_provider({**SETTINGS, "CHAT_PROVIDER": selected})
    assert str(caught.value) == "CHAT_PROVIDER: mock 또는 codyssey를 정확히 지정해 주세요."


@pytest.mark.parametrize("field", ["CODYSSEY_API_KEY", "CODYSSEY_BASE_URL", "CODYSSEY_MODEL"])
def test_codyssey_opt_in_requires_all_approved_settings(field):
    values = {**SETTINGS, field: ""}
    with pytest.raises(GptSettingsError):
        get_provider(values)


def test_codyssey_selection_constructs_the_adapter_without_making_a_call():
    with patch("app.integrations.codyssey._Call", side_effect=AssertionError("no request on selection")):
        provider = get_provider(SETTINGS)
    assert isinstance(provider, CodysseyGptProvider)
    assert provider.settings.base_url == APPROVED_BASE_URL and provider.settings.model == APPROVED_MODEL
    assert SETTINGS["CODYSSEY_API_KEY"] not in repr(provider.settings)


@pytest.mark.parametrize("selected", [None, "mock", "codyssey"])
def test_worker_startup_uses_the_selected_provider(auth_settings, monkeypatch, selected):
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)
    if selected is None:
        monkeypatch.delenv("CHAT_PROVIDER")
    else:
        monkeypatch.setenv("CHAT_PROVIDER", selected)
    monkeypatch.setenv("CHAT_WORKER", "on")
    application = create_app(auth_settings)
    worker = Mock()

    async def serve():
        async with lifespan(application):
            worker.start.assert_called_once_with()

    with patch("app.main.get_session_factory"), patch("app.main.ChatJobWorker", return_value=worker) as build:
        asyncio.run(serve())
    expected = CodysseyGptProvider if selected == "codyssey" else MockProvider
    assert isinstance(build.call_args.args[1], expected)
    worker.stop.assert_called_once()


@pytest.mark.parametrize("selected", ["invalid", "codyssey"])
def test_invalid_provider_configuration_prevents_worker_startup(auth_settings, monkeypatch, selected):
    monkeypatch.setenv("CHAT_WORKER", "on")
    monkeypatch.setenv("CHAT_PROVIDER", selected)
    monkeypatch.delenv("CODYSSEY_API_KEY", raising=False)
    application = create_app(auth_settings)

    async def serve():
        async with lifespan(application):
            pytest.fail("invalid opt-in must not serve with mock")

    with patch("app.main.get_session_factory"), patch("app.main.ChatJobWorker") as build:
        with pytest.raises((ProviderSettingsError, GptSettingsError)):
            asyncio.run(serve())
    build.assert_not_called()
