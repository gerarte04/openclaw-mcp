from app.common.models import TopNewsRequest
from rest_service.telegram_client import (
    ChannelInfo,
    TelegramNewsRepository,
    UnknownChannelSelectorError,
)


def channel(channel_id: int, title: str, username: str | None = None) -> ChannelInfo:
    return ChannelInfo(id=channel_id, title=title, username=username, entity=None)  # type: ignore[arg-type]


def test_selects_channel_by_username_and_id() -> None:
    channels = [channel(123, "Main News", "main_news"), channel(456, "Other")]
    request = TopNewsRequest(
        period="1d",
        included_channels=["@MAIN_NEWS", "-100456"],
    )
    selected, ignored = TelegramNewsRepository._select_channels(channels, request)
    assert [item.id for item in selected] == [123, 456]
    assert ignored == []


def test_exclusion_mode_ignores_missing_excluded_selector() -> None:
    channels = [channel(123, "Main News", "main_news"), channel(456, "Other")]
    request = TopNewsRequest(
        period="1d",
        exclude_mode=True,
        excluded_channels=["main_news", "does-not-exist"],
    )
    selected, ignored = TelegramNewsRepository._select_channels(channels, request)
    assert [item.id for item in selected] == [456]
    assert ignored == ["does-not-exist"]


def test_inclusion_mode_rejects_missing_selector() -> None:
    request = TopNewsRequest(period="1d", included_channels=["missing"])
    try:
        TelegramNewsRepository._select_channels([channel(123, "Main")], request)
    except UnknownChannelSelectorError:
        return
    raise AssertionError("Expected an unknown included channel to fail")
