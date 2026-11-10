# Panel answers

Answers the panel has pre-agreed. Quote them as written.

## The session

**How long do I have?** Four hours of wall-clock time. The session ends at the time `./interview start` printed, on the panel's clock; `./interview status` shows what is left. Starting late shortens it.

**Can I take a break?** Yes. The clock does not stop. Leave the computer awake and online: the recorder keeps running and uploads a checkpoint every 30 minutes.

**Can I submit early?** Yes: `./interview submit`. The session is frozen from then on.

**Will anyone contact me during the session?** No. Ask anything through `ask_panel` or `./interview ask`.

## The harness

**How many times am I asked for a decision?** Thirty. Days 61 through 90, in order, once each.

**Can I write the policy in something other than Python?** Yes. Ship an executable `policy` at the repository root that speaks the JSON-lines protocol in the brief. Any language. If you would rather stay in Python, `policy.py` with a `Policy` class is imported directly instead.

**If I ship a process, is it restarted for each day?** No. It is started once and stays alive for the whole run, so you can keep state in memory.

**Where does my logging go?** Anything a process policy writes to stderr is captured and shown to the panel. Do not write logs to stdout; that channel is the protocol.

**Is it the same instance throughout?** Yes, either way. State you keep survives between decisions.

**Is `history` cumulative?** Yes. Every row for every day strictly before the day being allocated, from the beginning of the account.

**Can rows I have already seen change between calls?** Yes. Values are as currently reported on the morning of the day being allocated, and rows for recent days may change in later calls.

**Is `daily_reported.csv` frozen row by row, or one snapshot?** One export of the whole table, pulled at the end of day 60, as the brief says.

**Is the history I receive the same data as the CSVs I was given?** No. Assume it is not. The harness supplies its own history through the `history` argument, covering the same 50 creatives. Use the CSVs to learn about the world, not as a stand-in for what you are handed at run time.

**What is `budget` in history?** For days 61 onward, what your policy allocated to that creative for that day, in dollars: its share after normalization, times $2,000. An allocation under $1 does not deliver and is recorded as 0. For earlier days, what the buyer allocated. `spend` is what the platform delivered.

**Is everything I allocate spent?** Not necessarily. A creative allocated less than $1 in a day does not deliver. Otherwise `spend` is what was delivered against the allocation, and anything not delivered on a day is not spent and is not carried over to another day or another creative.

**What if my shares do not sum to 1?** They are normalized. Negative values are an error. All zeros is treated as uniform.

**What if I return a `creative_id` that does not exist, or omit one?** Unknown ids are ignored. Omitted creatives get zero.

**Is the daily budget always $2,000?** Yes.

**Is there a time limit per decision?** Ten seconds per decision, ten minutes for a full thirty-day run. Exceeding either fails the run. A process policy is started before the clock on the first decision starts, so compilation or model loading at startup is free within thirty seconds.

**Can the policy read files at run time?** Yes, including the provided CSVs. Read them at startup where you can; the clock is per decision.

**Can the policy make network calls?** No. When we evaluate it, the policy runs with no network access. Anything it needs has to be in the repository.

**Can the policy write files?** It can, but runs repeat across seeds and may overlap. Do not carry state between runs.

**Will you run it on the seeds I tested?** No. Different seeds.

**Can I bake constants fitted from the given CSVs into the policy?** Yes. Be aware the environment we run you in is not the environment that produced those CSVs.

**Does the policy see competitor launches during days 61 to 90?** No. `history` is the only input after `init`.

## Scoring

**Which objective do you score me on?** Revenue over days 61 to 90, in our environment. We also report spend, impressions, clicks and purchases from the run. The objective you declare in `PREREG.md`, and your reason for it, are still part of what we read.

**Do I have to beat B2?** No. Say in your README where you landed and why.

**Am I penalized for not finishing?** No. Nobody finishes. You are read on what you chose and whether it holds up.

## Rules and tooling

**Which Python?** Only relevant if you ship `policy.py`, in which case 3.11 or later. A process policy has no such constraint.

**How are dependencies installed?** From a standard manifest, with `uv`. If it is not declared, it will not be there.

**Can I use any model, agent, or library?** Yes.

**Can I search the web?** Yes.

**Is `PREREG.md` really append-only?** Yes. Append timestamped entries. Do not rewrite earlier ones.

**Does asking questions here count against me?** No.

## Your tools

**Which tools does the recording read in full?** Claude Code and Codex. We read their chat history and their tool calls for this folder. GitHub Copilot in VS Code and Cursor are recorded too, but export each of their chats into this folder before the end as well, as a backup.

**What do I do for Claude Code?** The CLI and the VS Code extension are both recorded. Start Claude Code in this folder, not a parent folder. Trust the folder when it asks, and when it asks about the `interview-panel` server from `.mcp.json`, allow it; that is the panel.

**What do I do for Codex?** In the CLI, the desktop app or the VS Code extension: run `./interview setup` first. Then open Codex in this folder, trust the folder when it asks, and approve the interview's hooks: type `/hooks` in the Codex CLI, or open Settings > Hooks in the desktop app, and trust the ones from this folder. Codex asks again whenever they change, so do this after setup. We have not yet checked the hooks in the VS Code extension: if you use the extension, also export each chat into this folder before the end. Work in this folder, not in a worktree: if the app makes one, its changes count only once they are in this folder. Do not use `codex exec --ephemeral`, and do not delete a Codex chat until the session is over: neither leaves a record.

**What do I do for Copilot in VS Code?** Open this folder in VS Code and choose **Trust** when it asks; if you said no, run *Workspaces: Manage Workspace Trust*. Use the Chat view with **Local** or **Copilot** as the session target. Export each chat into this folder before the end: run *Chat: Export Chat...* and save it here. Inline chat (Cmd/Ctrl-I) is not saved: code it writes is allowed but, like inline completions, does not on its own show that a decision was yours. VS Code saves chats about once a minute: before you submit, wait a minute or close the window. If you let the time run out instead, its last minute may be missing, so export before then.

**What do I do for Cursor?** Use the editor, 3.22 or later, or `cursor-agent` in a terminal. Open this folder itself, and choose **Enable** when Cursor asks about the `interview-panel` MCP server. With `cursor-agent`, start it in this folder and trust the workspace. Run agents in this folder, not in a worktree or a best-of-N run: work there happens outside this folder. To redo a turn, send a new message: editing an earlier prompt, or restoring a checkpoint, deletes the later messages from Cursor's own record. Export each editor chat into this folder before the end: open the chat's **...** menu, choose **Export Chat**, and put the file here.

**How do I know my tool is being recorded?** After your first prompt, `./interview status` shows hook records for your tool. If it still shows 0, its hooks are not approved or not running: go through the steps for your tool again.

**Can I use another agent, editor or chat?** Yes. Export the chat into this folder before the end, or record a share link to the session with `./interview link <url>`. While a tool we cannot read is in use, changes in this folder count as source unknown, including lines you type yourself: they are not credited to you.

**Can I use inline completions?** Yes, and inline chat too. Code they may have written does not, on its own, show that a decision was yours. Where we cannot tell their lines from your typing, that holds for the lines you type too: once you use Copilot's chat or agent with inline suggestions on (the default), that is every line you type in the session, from its start; in Cursor's editor, it is when Cursor keeps no record of its completions. Say what you decided in a prompt or a commit message.

**I used a cloud agent or a web chat. What do I do?** Record each session as you go with `./interview link <url> [note]`, using a share link that anyone with it can open (Share, then copy link). A private chat URL shows us nothing. That includes a Codex cloud task, a Copilot Cloud session, and Cursor's cloud and background agents.

**I recorded the wrong link. Can I fix it?** Record the right one with `./interview link` again. Do not edit the files in `.interview/`.

**Does what I report have to be in the record?** Yes. Every result you report must be findable in the record. What we cannot find there counts as unverified.
