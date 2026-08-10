"""Telegram data access built on a pre-authorized Telethon session."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import Channel, Message

from app.common.config import Settings
from app.common.models import NewsPost, TopNewsRequest
from rest_service.scoring import calculate_interest_score

logger = logging.getLogger(__name__)


class TelegramAuthRequiredError(RuntimeError):
    """Raised when the configured session is unavailable or unauthorised."""


class UnknownChannelSelectorError(ValueError):
    """Raised when a requested channel selector cannot be resolved."""


@dataclass(frozen=True)
class ChannelInfo:
    id: int
    title: str
    username: str | None
    entity: Channel

    @property
    def selectors(self) -> set[str]:
        values = {str(self.id), f"-100{self.id}", self.title.casefold()}
        if self.username:
            values.update({self.username.casefold(), f"@{self.username.casefold()}"})
        return values


def parse_period(value: str) -> timedelta:
    match = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", value)
    if not match:
        raise ValueError(f"Unsupported period: {value}")
    weeks, days, hours, minutes = (int(part or 0) for part in match.groups())
    return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes)


class TelegramNewsRepository:
    """Reads channel posts with a temporary writable copy of the session file."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    async def get_top_news(self, request: TopNewsRequest) -> tuple[list[NewsPost], list[str], int]:
        if self.config.telegram_api_id <= 0 or not self.config.telegram_api_hash:
            raise TelegramAuthRequiredError("Telegram API credentials are not configured")
        session_file = self.config.telegram_session_file
        if not session_file.is_file():
            raise TelegramAuthRequiredError("Telegram session file is missing")

        # Telethon's SQLite session may update entity metadata. Keep the mounted
        # credential read-only and use a disposable copy for this request.
        with TemporaryDirectory(prefix="telegram-session-") as directory:
            temporary_session = Path(directory) / "telegram.session"
            shutil.copy2(session_file, temporary_session)
            client = TelegramClient(
                str(temporary_session.with_suffix("")),
                self.config.telegram_api_id,
                self.config.telegram_api_hash,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise TelegramAuthRequiredError("Telegram session is not authorized")
                return await self._get_top_news_with_client(client, request)
            finally:
                await client.disconnect()

    async def _get_top_news_with_client(
        self, client: TelegramClient, request: TopNewsRequest
    ) -> tuple[list[NewsPost], list[str], int]:
        channels = await self._list_channels(client)
        selected, ignored = self._select_channels(channels, request)
        cutoff = datetime.now(timezone.utc) - parse_period(request.period)
        ranking: list[NewsPost] = []
        started_at = asyncio.get_running_loop().time()
        scanned_channels = 0

        for channel in selected:
            remaining = self.config.telegram_query_timeout_seconds - (
                asyncio.get_running_loop().time() - started_at
            )
            if remaining <= 0:
                logger.warning("Telegram query deadline reached after %s channels", scanned_channels)
                break
            try:
                async with asyncio.timeout(
                    min(self.config.telegram_channel_timeout_seconds, remaining)
                ):
                    ranking.extend(await self._get_channel_posts(client, channel, cutoff))
                    scanned_channels += 1
            except (TimeoutError, RPCError) as error:
                logger.warning("Skipping Telegram channel %s: %s", channel.id, error)

        ranking.sort(key=lambda post: (post.interest_score, post.created_at), reverse=True)
        return ranking[:50], ignored, scanned_channels

    async def _list_channels(self, client: TelegramClient) -> list[ChannelInfo]:
        channels: list[ChannelInfo] = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, Channel) and entity.broadcast:
                channels.append(ChannelInfo(entity.id, entity.title, entity.username, entity))
        return channels

    @staticmethod
    def _select_channels(
        channels: list[ChannelInfo], request: TopNewsRequest
    ) -> tuple[list[ChannelInfo], list[str]]:
        by_selector = {
            selector: channel for channel in channels for selector in channel.selectors
        }

        def resolve(selectors: list[str]) -> tuple[set[int], list[str]]:
            resolved: set[int] = set()
            ignored: list[str] = []
            for selector in selectors:
                channel = by_selector.get(selector.strip().casefold())
                if channel is None:
                    ignored.append(selector)
                else:
                    resolved.add(channel.id)
            return resolved, ignored

        excluded_ids, excluded_ignored = resolve(request.excluded_channels)
        included_ids, included_ignored = resolve(request.included_channels)
        if not request.exclude_mode and included_ignored:
            raise UnknownChannelSelectorError(
                f"Included channels not found: {', '.join(included_ignored)}"
            )
        if request.exclude_mode:
            return [channel for channel in channels if channel.id not in excluded_ids], excluded_ignored
        return [channel for channel in channels if channel.id in included_ids], included_ignored

    async def _get_channel_posts(
        self, client: TelegramClient, channel: ChannelInfo, cutoff: datetime
    ) -> list[NewsPost]:
        """Fetch one history window to derive both channel baseline and ranked posts."""
        week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        history_cutoff = min(cutoff, week_cutoff)
        views: list[int] = []
        messages: list[Message] = []
        async for message in client.iter_messages(channel.entity):
            if message.date <= history_cutoff:
                break
            if message.date > week_cutoff and message.views is not None:
                views.append(max(0, message.views))
            if message.date > cutoff:
                messages.append(message)

        average_views = sum(views) / len(views) if views else 0.0
        return [
            post
            for message in messages
            if (post := self._to_news_post(message, channel, average_views)) is not None
        ]

    def _to_news_post(
        self, message: Message, channel: ChannelInfo, average_views: float
    ) -> NewsPost | None:
        text = (message.message or "").strip()
        if not text or message.id is None or message.date is None:
            return None

        reactions = sum(result.count for result in (message.reactions.results if message.reactions else []))
        comments = message.replies.replies if message.replies else 0
        shares = message.forwards or 0
        views = message.views or 0
        score = calculate_interest_score(
            reactions=reactions,
            comments=comments,
            shares=shares,
            views=views,
            average_views=average_views,
            created_at=message.date,
            smoothing_k=self.config.interest_smoothing_k,
            decay_lambda=self.config.interest_decay_lambda,
            reaction_weight=self.config.interest_reaction_weight,
            comment_weight=self.config.interest_comment_weight,
            share_weight=self.config.interest_share_weight,
        )
        if channel.username:
            url = f"https://t.me/{channel.username}/{message.id}"
        else:
            url = f"https://t.me/c/{channel.id}/{message.id}"
        return NewsPost(
            text=text,
            url=url,
            created_at=message.date,
            interest_score=score,
            channel_id=channel.id,
            channel_title=channel.title,
            message_id=message.id,
        )
