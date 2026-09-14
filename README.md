# Factory

Factory is a small, remotely controlled runtime for coding agents. Every channel is
an actual tmux window, so the server always reflects changes made by Factory, an
agent, another process, or a developer using tmux directly.

## Setup

Copy or clone this repository onto a Linux VM or macOS host, then run:

```console
./scripts/setup.sh
```

The bootstrap installs `uv` and Factory itself when necessary. Setup then detects the
package manager; installs tmux, Git, curl, a native build toolchain, Node.js, npm, and
the Codex CLI when they are missing; generates a TLS certificate; and installs a
restart-on-failure systemd or launchd service. The certificate path is printed for
copying to control clients.

Factory binds to localhost by default. Use an SSH tunnel for remote control:

```console
ssh -L 8443:127.0.0.1:8443 your-vm
```

This keeps authentication at the SSH boundary. An explicitly configured non-local
bind address should only be used behind a trusted network boundary.

For development, generate a certificate and start the server directly:

```console
uv run python scripts/key-gen.py
uv run factory start
```

The server recovers or creates the `factory` tmux session at startup.

## Control CLI

All state is read live from tmux. Channel identifiers such as `@1` are stable tmux
window identifiers; window names are accepted as well.

```console
factory state
factory create api-agent
factory send @1 'Inspect the failing tests and fix them'
factory read @1 --lines 300
factory notifications
factory notifications --follow
```

Remote commands accept `--host`, `--port`, `--certificate`, and `--server-name`.

## Protocol

The TLS socket uses newline-delimited, compact JSON-RPC 2.0. It supports requests,
notifications, batches, concurrent clients, and unilateral server notifications.
The public methods are:

| Method | Access | Parameters |
| --- | --- | --- |
| `factory.state` | Read | none |
| `channel.create` | Write | `{"name": string}` |
| `mailbox.send` | Write | `{"channel": string, "message": string}` |
| `mailbox.read` | Read | `{"channel": string, "lines"?: integer}` |
| `notification.list` | Read | `{"after"?: integer}` |
| `notification.subscribe` | Read stream | `{"after"?: integer}` |

Subscriptions first return durable event history, then emit JSON-RPC notifications
with method `factory.notification`. Events are appended and flushed before live
delivery, so clients can reconnect with the last sequence number without losing
events.

The generic test client can call any method directly:

```console
uv run python scripts/tcp_client.py localhost .keys/cert.pem \
  --method mailbox.read --params '{"channel":"@1","lines":50}'
```
