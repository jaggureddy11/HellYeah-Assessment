# Interview kit

Set this up before the session. It takes a minute and tells you nothing about the
exercise, which stays sealed until the panel gives you a passphrase.

## Before the day

```
./interview setup          # macOS, Linux, or WSL on Windows
```

It checks git, python and openssl, reaches the panel's server, wires the panel into your coding agent for
this machine, makes sure the session recorder runs here, and triggers any
operating-system permission prompts now rather than mid-session. If anything
fails, tell us before the day. On Windows, run everything inside WSL; native
Windows is not supported.

## On the day

```
./interview start <passphrase>
```

That opens the exercise, starts the recorder, and starts your 240 minutes of
working time, which you can split across sittings:

    ./interview pause      stops your clock
    ./interview resume     starts it again

Both need an internet connection. The clock keeps running unless you pause it,
through a closed laptop too. Do not work on the exercise while paused: the
recording shows what changed and when. Work on this computer, in this folder.

Packaging and upload happen by themselves when your working time is up.
Everything must be finished by 2026-11-16T05:07:13+00:00 (UTC). Do not count
on being submitted while paused: run `./interview submit` before then. If you
are paused at that time, what is in this folder is submitted only if this
computer is on, online and the recorder is running (after a restart, run
`./interview status` once); otherwise only the checkpoints reach us.

The exercise is opened by the panel's server, which starts the clock the first
time it accepts the passphrase: `start` needs an internet connection, and
running it again later picks up the same clock rather than a new one. Run it
when you are ready to begin, not before. The server opens this session until
2026-11-16T01:07:13+00:00 (UTC); start before then.

Your agent can ask the panel directly: it has an `ask_panel` tool and a
`time_left` tool. From a terminal the same two are `./interview ask "..."` and
`./interview status`.

If your machine sleeps or restarts, the recorder comes back by itself the next
time your agent or `./interview status` runs. Sleep does not stop your clock;
only `./interview pause` does, and `./interview resume` is only for starting the
clock again after `./interview pause`.

## What you may use

Any coding agent, model, editor, language or package manager. Some are recorded
more fully than others; see "Your tools". A cloud agent or a web chat keeps its
record on the provider's servers, not in this folder: as you go, record a share
link to each session, one anyone with it can open, with `./interview link <url>`.

## Your tools

Recorded in full: Claude Code and Codex. Recorded, but export each chat into
this folder before the end as well, as a backup: GitHub Copilot in VS Code, and
Cursor. Set up the one you will use before the day:

- Claude Code (the CLI or the VS Code extension): start it in this folder, not
  a parent folder. Trust the folder when it asks, and when it asks about the
  `interview-panel` server from `.mcp.json`, allow it; that is the panel.
- Codex (the CLI, the desktop app or the VS Code extension): run
  `./interview setup` first, then open Codex in this folder and trust the
  folder. Then approve the interview's hooks: `/hooks` in the CLI, or Settings >
  Hooks in the desktop app. Codex asks again whenever they change, so do this
  after setup. If you use the VS Code extension, also export each chat into this
  folder before the end. Work in this folder, not in a worktree.
- Copilot in VS Code: open this folder and choose Trust when VS Code asks. Use
  the Chat view with Local or Copilot as the session target. Export a chat with
  *Chat: Export Chat...*.
- Cursor (the editor, 3.22 or later, or `cursor-agent`): open this folder
  itself, and choose Enable when Cursor asks about the `interview-panel` MCP
  server. Run agents in this folder, not in a worktree or a best-of-N run. To
  redo a turn, send a new message. Export a chat from the chat's ... menu.

On the day, after your first prompt, `./interview status` shows hook records
for your tool; 0 means its hooks are not approved or not running.

Any other agent, editor or chat is allowed: export the chat into this folder
before the end, or record a share link with `./interview link <url>`. While a
tool we cannot read is in use, changes in this folder count as source unknown,
including lines you type yourself: they are not credited to you.

## What is recorded

A snapshot of the working tree every 20 seconds into
`.interview/snapshots.git`, which is separate from your own repository and never
touches your commits. At the end we gather the chat history your tools kept for
this project, and nothing from your other work.

Every 30 minutes, a copy of the recording so far (the snapshots, your agent
chat history for this folder and the session logs) is uploaded as a checkpoint. If
your machine is offline it is sent when you reconnect.

What is not sent: build output. `dist/` and `target/` at the top of this folder, and
`.venv/`, `__pycache__/` and `node_modules/` anywhere in it, are neither recorded nor
packaged. Nor is any `.git` directory: your commits travel as a bundle of the whole
history instead, so commit as much as you like. Keep work you want read outside those
directories; everything else in this folder goes, whether or not git tracks it.

All of this is done by Python under `.interview/`, plus the small hook and panel
files setup writes for each tool (in `.claude/`, `.codex/`, `.cursor/`,
`.github/hooks/` and `.vscode/`, and `.mcp.json`). Read them. Please do not
disable or remove them.
