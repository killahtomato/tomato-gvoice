# AGENTS.md — Tomato-gVoice Project

## Critical Rules

### GitHub & Backup
- **NEVER** push to GitHub without explicit user permission
- **NEVER** commit to GitHub without explicit user permission
- **NEVER** update/overwrite the local backup (`~/Backups/tomato-gvoice/`) without explicit user permission
- Always confirm with the user before any `git push`, `git commit`, or backup `rsync`/`cp` operation

### Code Changes
- Prefer minimal, targeted edits — avoid rewriting large sections unnecessarily
- Keep all subcommands (`login`, `status`, `threads`, `read`, `recent`, `send`, `contacts`, `search`, `watch`, `tui`, `tui-login`, `watchdog`, `kill`) intact unless explicitly asked to remove
- The TUI sidebar uses `ListView` with `ListItem(Static(label))` — this is the working pattern; do not replace with custom widgets unless necessary

### Architecture
- `src/tgv/` contains all source modules (config, session, reader, sender, contacts, watch, tui, cli)
- Watch and watchdog are separate systemd services that coordinate via `tui.lock`
- TUI uses a separate browser profile (`TUI_PROFILE_DIR`) from the main watch (`PROFILE_DIR`)
- Notifications use `omarchy-notification-send --exec` for click-to-open functionality
- The `tgv` launcher symlinked at `~/.local/bin/tgv` is the entry point

### Testing
- Project venv is at `.venv/` (use `.venv/bin/python` or `PYTHONPATH=src`)
- The TUI renders in terminal alternate screen mode — stdout capture will show only escape sequences, not rendered content
- Test TUI changes by running `tgv tui` interactively, not via stdout capture
