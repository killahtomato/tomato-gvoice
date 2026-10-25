# Tomato-gVoice (`tgv`)

A terminal CLI for Google Voice SMS. Send and read text messages from your shell — no app, no website.

> Unofficial, not affiliated with or endorsed by Google. Google publishes no public API for Google Voice, so `tgv` drives the web app in a persistent Chromium profile.

## Features

- **Send SMS** from the terminal
- **Read** conversations by index or name
- **List** recent messages in a time window
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
```

Add `--json` to any command for machine-readable output:

```bash
tgv recent 30m --json
```

### Options

| Option | Description |
|---|---|
| `--headed` | Run the browser in a visible window instead of headless |
| `--json` | Machine-readable output |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TGV_HOME` | `~/.config/tomato-gvoice` | Config directory |
| `TGV_PROFILE_DIR` | `$TGV_HOME/chrome` | Browser profile (your login) |
| `TGV_CHROME` | auto-detected | Chromium executable to use |
| `TGV_DEBUG` | unset | Print step-by-step send logs |

## How it works

`tgv` launches a persistent Chromium profile against `voice.google.com`:

- **Reading** intercepts the web app's internal `api2thread/list` JSON and decodes it (falling back to the DOM for previews).
- **Sending** drives the composer UI and waits for the real `sendsms` API response before reporting success.

The signed-in session lives in `~/.config/tomato-gvoice/chrome` — treat it as a login.

## Caveats

- Google can change the web app's DOM or endpoints at any time; a selector may need updating.
- Use `--headed` if headless mode is ever blocked.
- This is a personal-use tool and is not affiliated with Google.

## License

MIT
