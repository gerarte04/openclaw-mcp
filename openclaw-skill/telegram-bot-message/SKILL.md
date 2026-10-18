---
name: telegram-bot-message
description: Send a message with text and optional media through the user's Telegram bot.
user-invocable: true
---

# Telegram bot message

Use the MCP tool `sendTelegramBotMessage` when the user asks to send, post, or
forward something to Telegram through their bot, for example to deliver a
digest or a playlist link to themselves or to a channel where the bot is an
administrator.

## Preconditions

The user must create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN` (and
optionally `TELEGRAM_BOT_DEFAULT_CHAT_ID`) in `.env`, then restart the
services with `docker compose up -d --build`. Never ask for, repeat, or store
the bot token. If the tool returns `TELEGRAM_BOT_NOT_CONFIGURED` or
`TELEGRAM_BOT_AUTH_ERROR`, tell the user to configure or fix these `.env`
values themselves.

## Tool arguments

- `text`: message text, up to 4096 characters. With media it becomes the
  caption when it fits in 1024 characters; otherwise it is sent as a separate
  message before the media.
- `media`: optional ordered list of up to 10 items
  `{"type": "photo" | "video" | "animation" | "audio" | "document", "url": "https://..."}`.
  URLs must be public: Telegram downloads them itself (photos up to 5 MB,
  other files up to 20 MB). Local file paths are not supported.
- `chat_id`: optional numeric chat ID or `@channel_username`. Omit it to use
  the configured default chat. Only pass it when the user names a target.
- `parse_mode`: optional `HTML` or `MarkdownV2`. With `HTML`, escape `<`, `>`
  and `&` in plain text. With `MarkdownV2`, escape every reserved character.
  Omit it for plain text.

At least one of `text` and `media` is required.

## Results

On success the tool returns `chat_id` and `message_ids`; confirm briefly that
the message was sent. If it returns `TELEGRAM_BOT_REJECTED`, relay Telegram's
description (for example, an unknown chat or an unreadable media URL) and
suggest a fix. When the error says some messages were already sent, do not
resend the whole message without asking the user. On `UPSTREAM_RATE_LIMIT`,
wait before retrying.
