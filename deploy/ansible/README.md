# Ansible deployment

`deploy.yml` deploys the REST service and the MCP server with Docker Compose
on a Debian or Ubuntu server:

1. installs Docker Engine and the Compose plugin (`install_docker: false`
   skips this);
2. copies the project to `app_dir` (`/opt/openclaw-mcp`);
3. renders `.env` from variables with `0600` permissions;
4. optionally uploads Telegram and Spotify authorization files created
   locally;
5. builds the images, starts the services, and checks `/healthz`, the OAuth
   metadata, and that `/mcp` refuses requests without a token.

## What is exposed

Only Caddy listens on the network, on ports 80 and 443 of every interface. It
serves `https://<mcp_domain>/mcp` with a certificate it obtains and renews
itself, and proxies to the MCP server. The MCP server requires OAuth: Claude
registers itself as a client and you approve it once with a password. The
REST service (8000) and the plain MCP port (8001) listen on `127.0.0.1` only.

Further protections:

- Clients may register only Claude's callback URLs
  (`https://claude.ai/api/mcp/auth_callback` and
  `https://claude.com/api/mcp/auth_callback`; change them with
  `mcp_oauth_allowed_redirect_uris`), so a sign-in link cannot send an
  authorization code anywhere else.
- Access tokens last an hour; refresh tokens last 90 days and are rotated on
  every use. The server stores only their SHA-256 digests, in
  `data/oauth/state.json`, which survives restarts.
- Five wrong passwords lock the sign-in page for 15 minutes. The lock is
  global, so someone guessing can also keep you out for that time, but never
  in.
- The MCP container mounts only `data/oauth`; the Telegram session and the
  Spotify token stay in the REST container.
- The bot sends only to `telegram_bot_default_chat_id` and
  `telegram_bot_allowed_chat_ids`, so a Telegram post that asks the agent to
  message someone else cannot make it do so.

Docker publishes ports through its own iptables rules, so `ufw` rules alone
do **not** block them. Keep `rest_bind_address` and `mcp_bind_address` on
`127.0.0.1` unless you add your own protection.

## Requirements

On the control machine:

```bash
pip install ansible-core
ansible-galaxy collection install -r requirements.yml
```

On the server: Debian or Ubuntu, SSH access, and a user with `sudo`.

## Configuration

Run the commands below from `deploy/ansible`.

```bash
cp inventory.example.ini inventory.ini
# Set the server address and SSH user.

cp vault.example.yml group_vars/all/vault.yml
# Fill in the Telegram, Spotify, Last.fm, and bot credentials and the OAuth
# password (openssl rand -base64 24).
ansible-vault encrypt group_vars/all/vault.yml
```

Set `mcp_domain` in `group_vars/all/main.yml` (the placeholder is
`example.com`) and point the domain's DNS A/AAAA record at the server. Ports
80 and 443 must be reachable from the internet so Caddy can obtain the
certificate. `public_mcp: false` deploys without Caddy and without OAuth.

`inventory.ini` and `group_vars/all/vault.yml` are git-ignored. Other settings
are in `group_vars/all/main.yml`: addresses and ports, `app_dir`, and
`app_extra_env` for additional `.env` values such as scoring weights.

## Deploy

```bash
ansible-playbook deploy.yml --ask-vault-pass --ask-become-pass
```

Run the same command again to deploy new code: it copies the sources,
rebuilds the images, and recreates changed containers. The `data/` directory
on the server (Telegram session, Spotify refresh token) is kept.

## Connecting Claude

In Claude, open [claude.ai/customize/connectors](https://claude.ai/customize/connectors),
add a custom connector, and enter `https://<mcp_domain>/mcp`. Leave the OAuth client ID and secret empty: Claude
registers itself. Claude opens the server's sign-in page; enter
`vault_mcp_oauth_password` and choose **Allow access**. From then on Claude,
including its scheduled tasks, uses and refreshes the tokens itself.

To disconnect every client, delete `data/oauth/state.json` on the server and
restart `mcp-server`; changing the password does not revoke issued tokens.

## Authorization

Deploy first, then authorize. `authorize.yml` asks for everything in your
terminal and runs the authorization scripts inside the deployed `rest-server`
container, so you never open an SSH session yourself:

```bash
ansible-playbook deploy.yml --ask-vault-pass --ask-become-pass
ansible-playbook authorize.yml --ask-vault-pass --ask-become-pass
```

It fails with "run deploy.yml first" if the services are not running. Use
`--tags telegram` or `--tags spotify` to authorize only one service. A service
that is already authorized is skipped; set `telegram_reauthorize: true` or
`spotify_reauthorize: true` (for example with `-e`) to authorize it again,
such as after new Spotify scopes are added.

**Telegram.** The playbook asks for the phone number (or uses
`telegram_phone`), sends the login code, asks for the code, and asks for the
two-step verification password if the account has one; the password is not
echoed. The new session replaces the old one only after sign-in succeeds.

**Spotify.** The playbook prints an authorization URL. Open it in your local
browser and allow access. The browser is then redirected to
`http://127.0.0.1:8888/callback?code=...&state=...`, which does not load
because nothing listens there; this is expected. Copy the full address from
the address bar into the prompt. The playbook checks the state and saves the
refresh token on the server. Do this within 15 minutes of starting.

The phone number, code, password, and redirected address reach the scripts on
stdin and are hidden from Ansible's output (`no_log`). The services pick up
the new files on the next request; no restart is needed.

### Uploading files created locally

If you already authorized on your machine (see the main
[README](../../README.md)), upload the files instead:

```bash
ansible-playbook deploy.yml --ask-vault-pass --ask-become-pass \
  -e telegram_session_local_path=../../data/telegram.session \
  -e spotify_token_local_path=../../data/spotify/refresh-token.json
```

Existing files on the server are not overwritten, because Spotify may rotate
the refresh token there. Add `-e overwrite_auth_files=true` to replace them.
