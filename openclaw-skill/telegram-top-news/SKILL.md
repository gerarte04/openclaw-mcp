---
name: telegram-top-news
description: Get the most interesting recent posts from the user's Telegram channels.
user-invocable: true
---

# Telegram top news

Use the MCP tool `getTopNews` to retrieve up to 50 ranked posts from the
current user's Telegram channels. This skill is available both as
`/telegram-top-news` and to the agent when it needs recent Telegram news.

Unless the user asks for another interval, make a weekly digest with
`period: "1w"`. Unless they restrict the channels, search all available
channels with `exclude_mode: true`, empty `excluded_channels`, and empty
`included_channels`.

## Preconditions

The user must manually authorize Telegram before using the tool:

```bash
cp .env.example .env
# Fill TELEGRAM_API_ID and TELEGRAM_API_HASH from my.telegram.org.
docker compose run --rm --no-deps -v "$(pwd)/data:/app/data" \
  rest-server python scripts/authorize_telegram.py
docker compose up -d --build
```

Do not ask the user for a Telegram verification code, two-factor password, or
other credentials. Do not try to authorize Telegram from OpenClaw.

If the tool returns `TELEGRAM_AUTH_REQUIRED`, clearly tell the user that
Telegram is not authorized and ask them to manually run:

```bash
docker compose run --rm --no-deps -v "$(pwd)/data:/app/data" \
  rest-server python scripts/authorize_telegram.py
```

The script must be run from the project directory where `.env` is configured.

## User preferences and channel filters

Treat the user's channel and content choices as durable preferences when the
agent has access to persistent memory. Save the preference in concise,
actionable form and reuse it for later Telegram digests until the user changes
or removes it.

- If the user says they do not want posts from specific channels, remember
  those selectors as exclusions and call the tool with `exclude_mode: true`.
- If the user says they want posts only from specific channels, remember them
  as inclusions and call the tool with `exclude_mode: false`.
- If both a saved preference and the current request specify channels, the
  current request wins. Ask only when the instruction is genuinely ambiguous.
- If the user asks to reset or include everything again, forget the saved
  channel restrictions and use the all-channels default.
- Save explicit content preferences as well: subjects, sources, formats, and
  signals the user calls interesting or uninteresting. Apply them when
  selecting the final digest, but do not change the MCP query solely because
  of a content preference.

## Tool arguments

Call `getTopNews` with these arguments:

- `period` (required): compact non-zero duration. Supported units are `w`
  (weeks), `d` (days), `h` (hours), and `m` (minutes). Examples: `1w`, `1d`,
  `9h32m`, `1w2d`. Use `1w` when the user did not give a period.
- `exclude_mode` (default for this skill is `true`):
  - `false`: search only `included_channels`; at least one item is required.
  - `true`: search every Telegram channel available to the user except
    `excluded_channels`.
- `included_channels`: channel selectors when `exclude_mode` is `false`.
- `excluded_channels`: channel selectors when `exclude_mode` is `true`.

Each channel selector may be a channel title, username, `@username`, bare
Telegram channel ID, or a `-100`-prefixed ID. The title and username matching
is case-insensitive. Empty selector arrays are allowed only where their mode
permits them: an empty `excluded_channels` in exclusion mode means all
channels; an empty `included_channels` is invalid in inclusion mode.

## Calling and writing a digest

The tool returns candidates, not a ready-to-send digest. After every successful
call, independently select approximately 10–15 posts:

1. Start with the highest `interest_score`.
2. Then apply saved and stated content preferences: favor topics, sources, and
   formats the user finds useful; omit content they find uninteresting.
3. Prefer diversity when several posts repeat the same news. Do not invent
   details that are absent from the source post.
4. Preserve the central fact and important context, but rewrite the text into
   a concise digest item when useful.
5. Every included item must have a clickable Markdown link whose anchor is a
   meaningful phrase, not a raw URL.

Write in the user's language and use this shape (the wording may vary):

```markdown
Вот сводка самых интересных постов за последнюю неделю:
1. Позавчера молния [ударила в Останкинскую башню](https://t.me/example/1).
   Во всём городе наблюдались помехи в работе телевидения.
2. Автор канала DeepLearning опубликовал [визуализацию обучения с подкреплением](https://t.me/example/2).
```

State the actual requested period in the introduction. If there are fewer than
10 genuinely useful posts, return fewer rather than padding the digest.

## Examples

Default request (all channels, last week):

```json
{
  "period": "1w",
  "exclude_mode": true,
  "excluded_channels": [],
  "included_channels": []
}
```

News from two named channels in the past day:

```json
{
  "period": "1d",
  "exclude_mode": false,
  "included_channels": ["@telegram", "My News Channel"],
  "excluded_channels": []
}
```

News from all channels except two over the last 9 hours 32 minutes:

```json
{
  "period": "9h32m",
  "exclude_mode": true,
  "excluded_channels": ["noisy_channel", "-1001234567890"],
  "included_channels": []
}
```

## Results and limitations

The result contains `posts`, sorted by descending `interest_score`. Each post
has `text`, `url`, `created_at`, `interest_score`, channel metadata, and a
message ID. Ranking uses reactions, comments, forwards, views relative to the
channel's seven-day average, and freshness.

Only channels accessible to the Telegram account in the local session can be
searched. Public links work normally. Private `t.me/c/...` links work only for
accounts that are members of that channel.
