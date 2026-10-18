# Ansible deployment

`deploy.yml` deploys the REST service and the MCP server with Docker Compose
on a Debian or Ubuntu server:

1. installs Docker Engine and the Compose plugin (`install_docker: false`
   skips this);
2. copies the project to `app_dir` (`/opt/openclaw-mcp`);
3. renders `.env` from variables with `0600` permissions;
4. optionally uploads Telegram and Spotify authorization files;
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

The services need the manual authorizations described in the main
[README](../../README.md). Do either of the following.

**Upload files created locally.** After authorizing on your machine, set the
paths in `group_vars/all/main.yml` or pass them with `-e`:

```bash
ansible-playbook deploy.yml --ask-vault-pass --ask-become-pass \
  -e telegram_session_local_path=../../data/telegram.session \
  -e spotify_token_local_path=../../data/spotify/refresh-token.json
```

Existing files on the server are not overwritten, because Spotify may rotate
the refresh token there. Add `-e overwrite_auth_files=true` to replace them.

**Authorize on the server.** Telegram authorization is interactive in the
terminal:

```bash
ssh -t deploy@<server> 'cd /opt/openclaw-mcp && sudo docker compose run --rm telegram-auth'
```

Spotify's redirect URI is `http://127.0.0.1:8888/callback`, so forward that
port over SSH and open the printed URL in your local browser:

```bash
ssh -t -L 8888:127.0.0.1:8888 deploy@<server> \
  'cd /opt/openclaw-mcp && sudo docker compose --profile spotify-auth run --rm --service-ports spotify-auth'
```

The services pick up the new files on the next request; no restart is needed.
