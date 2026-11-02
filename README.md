# gj — effort tracker for coding agents

A local board for the work your Claude, Codex, and Grok sessions are doing,
so no PR gets forgotten. It has one SQLite file, one Python script (stdlib only), and a
dashboard served on localhost.

## Setup

```sh
./install.sh          # symlinks gj into ~/.local/bin
gj install            # tells Claude, Codex, and Grok to use gj (idempotent)
gj service install    # dashboard always on at http://gj.localhost:7777
```

`gj service install` registers a macOS LaunchAgent (`~/Library/LaunchAgents/co.wetstone.gj.plist`)
that runs `gj serve` at login and restarts it if it exits. It captures your current `PATH`
so the service can find `gh`. Logs go to `~/.local/share/gj/serve.log`. Use
`gj service status` to check on it and `gj service uninstall` to remove it. To run the
dashboard in the foreground instead, use `gj serve --open`.

`gj install` adds a short marked block to `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, and
`~/.grok/AGENTS.md`. For Codex it also adds the gj data directory to
`[sandbox_workspace_write] writable_roots` in `~/.codex/config.toml`, because Codex's
default sandbox only allows writes inside the workspace. `gj install --uninstall` removes
both.

## How it works

1. **The agent registers its work.** When a task will produce a PR, the agent runs
   `gj start "title" --prompt "<request>"`. gj figures out the rest itself:
   - **Agent and session ID.** Claude reads `$CLAUDE_CODE_SESSION_ID`. Codex reads the
     newest user thread for the cwd in `~/.codex/state_*.sqlite`. Grok matches the parent
     pid in `~/.grok/active_sessions.json`.
   - **Repo and branch.** These come from git in the cwd.
   - **Initial prompt.** If `--prompt` is omitted, gj reads it from the agent's own session logs.

   Running `gj start` again in the same session updates the effort instead of creating a
   duplicate.
2. **The agent links its PR.** After opening a PR, the agent runs `gj pr <url>`, which sets
   the status to `review`.
3. **gj syncs with GitHub.** `gj sync` runs every 10 minutes while the dashboard is up, and
   you can also run it by hand. It pulls PR state, checks, review decision, and
   mergeability through `gh`. It also:
   - **Finds unlinked PRs.** If an agent forgot `gj pr`, it finds PRs created on the
     effort's branch.
   - **Closes finished efforts.** When all of an effort's PRs are merged, the effort is
     marked `done`. If they were all closed unmerged, it is marked `dropped`.
4. **The dashboard shows what needs you.** Efforts with problems are sorted first and marked
   with an orange edge:
   - stale PR (no activity for `GJ_STALE_DAYS`, 3 by default)
   - failing checks
   - merge conflict
   - changes requested
   - approved but not merged
   - blocked
   - no PR and idle

   Each card has:
   - PR links with their status
   - a **Copy** button for the resume command, e.g.
     `cd ~/src/app && claude --resume <id>`, `codex resume <id>`, or `grok --resume <id>`
   - the initial prompt
   - buttons for status, note, and adding a PR

## CLI

```
gj start "title" [--prompt ..] [--epic ..] [--pr URL] [--new]
gj pr URL... [--id N]
gj status active|review|blocked|done|dropped [--note ..] [--id N]
gj note text... [--id N]          gj edit --id N [--title|--epic|--prompt]
gj ls [-a] [-s] [--json]          gj resume N          gj rm N
gj sync [--all]                   gj serve [--port 7777] [--open]
gj service install|uninstall|status [--port 7777] [--open]
```

Commands with no `--id` act on the effort registered by the calling agent session.

Statuses are `active`, `review`, `blocked`, `done`, and `dropped`. Epics are free text, and
the dashboard groups efforts by epic.

## Config

| env | default | |
|---|---|---|
| `GJ_DB` | `~/.local/share/gj/gj.db` | database path |
| `GJ_STALE_DAYS` | `3` | days without activity before a PR counts as stale |
| `GJ_PORT` | `7777` | dashboard port |

## Tests

```sh
python3 test_gj.py
```
