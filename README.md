# Tomato-gVoice (`tgv`)

A terminal CLI for Google Voice SMS. Send and read text messages from your shell — no app, no website.

> Unofficial, not affiliated with or endorsed by Google. Google publishes no public API for Google Voice, so `tgv` drives the web app in a persistent Chromium profile.

## Features

- **Send SMS** from the terminal
- **Read** conversations by index or name
- **List** recent messages in a time window
- **Search** messages by text
- **Contact name resolution** — `tgv send recipient "..."` instead of typing a number
- **Watch** — desktop notifications when new messages arrive
- **TUI** — interactive full-screen terminal interface (`tgv tui`)
- **Clickable links** — URLs in messages are colored, underlined, and open in your browser on click
- **Watchdog** — background service that manages notifications while the TUI is closed
- **Kill** — clean up all tgv processes
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

## Quick start

```bash
tgv login                    # one-time interactive sign-in for the CLI
tgv tui-login                # one-time sign-in for the TUI (separate browser profile)
tgv status                   # check the saved session
tgv tui                      # open the interactive terminal UI
```

## Commands

### Listing & reading

```bash
tgv threads                  # list conversation previews
tgv threads -n 20            # limit to 20 threads
tgv threads --json           # machine-readable output
tgv read 3                   # read a thread by index
tgv read "Contact Name"      # ...or by name
tgv read 5 -n 50             # limit to 50 messages
tgv recent 24h               # messages in the last 24 hours
tgv recent 7d --incoming     # only incoming messages
tgv recent 30m -n 10         # limit to 10 messages
```

### Sending

```bash
tgv send "(555) 555-5555" "hello"
tgv send recipient "hello"   # send to a contact by name
tgv send recipient "are we still on for tonight?"
tgv send recipient "hello" --dry-run   # preview without sending
tgv send recipient "hello" --refresh   # re-fetch contacts first
```

### Search & contacts

```bash
tgv search "pizza"                    # search all messages
tgv search tacos --thread recipient   # ...within one conversation
tgv search sushi -n 20                # limit to 20 results
tgv contacts                          # list known contacts
tgv contacts Contact                  # search contacts
tgv contacts --refresh                # re-fetch contacts
```

### Notifications

```bash
tgv watch                # notify every 30s until Ctrl+C
tgv watch --interval 10  # check every 10s
tgv watch --once         # single check (for cron)
tgv watch --background   # detach into the background
tgv watch --status       # show watch service status
tgv watch --json         # print notifications as JSON
```

### Interactive TUI

```bash
tgv tui                  # open the interactive terminal UI
tgv tui --headed         # show the browser window
tgv tui --interval 12    # poll every 12 seconds
tgv tui --verbose        # print debug logs to stderr
```

Layout: sidebar (conversations) on the left, messages on the right, a compose
box at the bottom. Outgoing messages align right, incoming align left.

**Clickable links:** URLs in message bubbles are automatically highlighted
(colored and underlined). Hovering over a link highlights it, and clicking it
opens the URL in your default browser.

Keys:

| Key | Action |
|---|---|
| `j` / `k` or arrows | Navigate conversations |
| `Enter` | Open conversation |
| `i` | Focus the compose box |
| `Esc` | Back to conversation list |
| `r` | Refresh |
| `c` | Compose a new message |
| `/` | Search |
| `g` | Contacts |
| `m` | Mark as read |
| `?` | Help |
| `q` | Quit |

New messages trigger pop-up alerts while the TUI is running.

### Cleanup

```bash
tgv kill                 # kill all tgv processes (watch, watchdog, TUI, chromium)
```

## Sending to a contact by name

`send` accepts a name as well as a phone number:

```bash
tgv send recipient "are we still on for tonight?"
```

Names are matched exactly first, then by prefix, then by substring. If a name
matches several contacts, `tgv` lists them instead of guessing:

```
$ tgv send "Contact Name" "hi"
error: Multiple contacts match 'Contact Name': +1 (555) 555-5555, +1 (555) 555-5556
```

Known contacts come from your existing conversations plus your Google contact
list, cached in `~/.config/tomato-gvoice/contacts.json`. Use `--refresh` to
re-fetch.

## Background services

Two systemd user services coordinate to keep notifications running:

### `tgv-watch`

Polls your inbox and sends a desktop notification for each new message. When the TUI is **not** running, it keeps you notified in the background.

```bash
systemctl --user enable --now tgv-watch
```

This starts on login and restarts on failure. When you open `tgv tui`, the service is automatically stopped (so the TUI can use the browser); when you quit the TUI, the service restarts.

```bash
systemctl --user status tgv-watch
systemctl --user stop tgv-watch
journalctl --user -u tgv-watch -f
```

### `tgv-watchdog`

Manages the lock file that coordinates between the TUI and the watch service. Runs as a separate service that pauses the watcher while the TUI is open.

```bash
systemctl --user enable --now tgv-watchdog
```

```bash
systemctl --user status tgv-watchdog
journalctl --user -u tgv-watchdog -f
```

When the TUI exits, it automatically starts the `tgv-watch` service so notifications resume. Clicking a notification reopens the TUI.

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
