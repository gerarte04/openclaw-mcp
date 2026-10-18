# Ansible deployment

`deploy.yml` deploys the REST service and the MCP server with Docker Compose
on a Debian or Ubuntu server:

1. installs Docker Engine and the Compose plugin (`install_docker: false`
   skips this);
2. copies the project to `app_dir` (`/opt/openclaw-mcp`);
3. renders `.env` from variables with `0600` permissions;
4. optionally uploads Telegram and Spotify authorization files created
   locally;
5. builds the images, starts the services, and checks `/healthz` and the MCP
   port.

Both services listen on every network interface (`0.0.0.0`):

- REST: `http://<server>:8000`
- MCP: `http://<server>:8001/mcp`

> **Warning.** Neither service has authentication. Anyone who can reach these
> ports can read your Telegram channels, send messages as your bot, and create
> playlists in your Spotify account. Restrict access with a cloud security
> group or firewall, a VPN, or set `rest_bind_address`/`mcp_bind_address` to
> a private interface address. Docker publishes ports through its own iptables
> rules, so `ufw` rules alone do **not** block them.

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
# Fill in the Telegram, Spotify, Last.fm, and bot credentials.
ansible-vault encrypt group_vars/all/vault.yml
```

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
