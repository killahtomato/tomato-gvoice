# Tomato-gVoice (`tgv`)

A terminal CLI for Google Voice SMS. Send and read text messages from your shell — no app, no website.

> Unofficial, not affiliated with or endorsed by Google. Google publishes no public API for Google Voice, so `tgv` drives the web app in a persistent Chromium profile.

## Features

- **Send SMS** from the terminal
- **Read** conversations by index or name
- **List** recent messages in a time window
- **Search** messages by text
- **Contact name resolution** — `tgv send Ricki "..."` instead of typing a number
- **Watch** — desktop notifications when new messages arrive
- **JSON output** on every command for scripting

## Requirements

- Python 3.11+
- A Chromium-based browser installed (`chromium`, `google-chrome`, etc.)
- A Google account with a Google Voice number

## Install

```bash
git clone https://github.com/killahtomato/tomato-gvoice.git
cd tomato-gvoice
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then make the `tgv` command available:

```bash
ln -sf "$PWD/tgv" ~/.local/bin/tgv
```

## Usage

```bash
tgv login                    # one-time interactive sign-in (opens a browser window)
tgv status                   # check the saved session
tgv threads                  # list conversation previews
tgv read 3                   # read a thread by index
tgv read "Nanny T"           # ...or by name
tgv recent 24h               # messages in the last 24 hours
tgv recent 7d --incoming     # only incoming messages
tgv send "(717) 429-6075" "hello"
tgv send Ricki "hello"       # send to a contact by name
tgv send Ricki "hello" --dry-run   # preview resolution without sending
tgv contacts                 # list known contacts
tgv contacts Rick            # search contacts
tgv search test              # search messages containing "test"
tgv search test --thread Ricki   # ...within one conversation
tgv watch                    # notify about new messages until Ctrl+C
tgv watch --interval 10      # check every 10 seconds
tgv watch --once             # check once (handy for cron)
```

Add `--json` to any command for machine-readable output:

```bash
tgv recent 30m --json
```

### Sending to a contact by name

`send` accepts a name as well as a phone number:

```bash
tgv send Ricki "are we still on for tonight?"
```

Names are matched exactly first, then by prefix, then by substring. If a name
matches several contacts, `tgv` lists them instead of guessing:

```
$ tgv send Dad "hi"
error: Multiple contacts match 'Dad': +17178775829, +17174337260
```

Known contacts come from your existing conversations plus your Google contact
list, cached in `~/.config/tomato-gvoice/contacts.json`. Use `--refresh` to
re-fetch.

### Searching

`search` does a case-insensitive substring match across the loaded threads:

```bash
tgv search "pizza"
tgv search test --thread Ricki
tgv search test --json
```

### Watching for new messages

`watch` polls your inbox and sends a desktop notification for each new message:

```bash
tgv watch                 # every 30s, until Ctrl+C
tgv watch --interval 10   # check every 10s
tgv watch --once          # single check (for cron)
tgv watch --json          # print notifications instead of a popup
```

Each notification is clickable — click it to open the conversation in a
terminal (`foot --hold tgv read <n>`). Override the terminal with `--terminal`
or `TGV_TERMINAL`.

The first run seeds the state to "now", so old messages don't trigger a flood.
Notifications use `notify-send` when available, falling back to terminal output.

#### Running it in the background

`tgv watch` blocks the terminal while it runs. To keep it running without a
dedicated terminal, install the systemd user service:

```bash
systemctl --user enable --now tgv-watch
```

This starts on login and restarts on failure. Manage it with:

```bash
systemctl --user status tgv-watch
systemctl --user stop tgv-watch
journalctl --user -u tgv-watch -f
```

The service lives at `~/.config/systemd/user/tgv-watch.service`.

### Options

| Option | Description |
|---|---|
| `--headed` | Run the browser in a visible window instead of headless |
| `--json` | Machine-readable output |
| `--dry-run` | (send) Resolve the recipient and show where it would go without sending |
| `--refresh` | (send/contacts) Re-fetch contacts instead of using the cache |
| `--thread` | (search) Limit results to a contact/thread name |
| `--interval` | (watch) Seconds between checks (default 30) |
| `--once` | (watch) Check once and exit |
| `--terminal` | (watch) Terminal to open clicked notifications in |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TGV_HOME` | `~/.config/tomato-gvoice` | Config directory |
| `TGV_PROFILE_DIR` | `$TGV_HOME/chrome` | Browser profile (your login) |
| `TGV_CONTACTS_CACHE` | `$TGV_HOME/contacts.json` | Cached contact map |
| `TGV_WATCH_STATE` | `$TGV_HOME/watch_state.json` | Last-seen message timestamp |
| `TGV_CHROME` | auto-detected | Chromium executable to use |
| `TGV_DEBUG` | unset | Print step-by-step send logs |

## How it works

`tgv` launches a persistent Chromium profile against `voice.google.com`:

- **Reading** intercepts the web app's internal `api2thread/list` JSON and decodes it (falling back to the DOM for previews).
- **Sending** drives the composer UI and waits for the real `sendsms` API response before reporting success.
- **Contacts** are decoded from the thread payload (numbers) plus the DOM (display names and the full Google contact list), then cached.
- **Search/Watch** reuse the same decoded payload; `watch` tracks the last-seen timestamp in `watch_state.json` and sends desktop notifications.

The signed-in session lives in `~/.config/tomato-gvoice/chrome` — treat it as a login.

## Caveats

- Google can change the web app's DOM or endpoints at any time; a selector may need updating.
- Use `--headed` if headless mode is ever blocked.
- This is a personal-use tool and is not affiliated with Google.

## License

MIT
