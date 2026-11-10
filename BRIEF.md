# Creative Rotation Brain

**Take-home, 4 hours. There is no demo afterwards: we assess your repository and the session record.**

What we describe below is well over a week of work. You will finish part of it. Four hours is enough to go deep on the parts you choose, so depth is what we read: whether you can show us that your policy works, and whether you went looking for the evidence that would show it does not.

## The situation

We run paid social for DTC brands. Every account has 30 to 60 live creatives and a fixed daily budget. Today a human decides each morning how to split that budget across creatives, and when to give a brand-new creative a chance. We want to automate that decision.

You are building the brain of this feature: a policy that, every day, receives everything the reporting dashboard currently shows and returns a budget split. The account you are optimizing runs for 30 more days at $2,000 a day. That is $60,000 total, and it does not renew. To develop and evaluate it you will also build a simulated delivery environment, because we cannot run experiments on real spend.

## What you get

Four synthetic files shaped on real accounts. Currency is USD. Budget is $2,000 per day. Day 1 is a Monday.

| File | Contents |
|---|---|
| `creatives.csv` | 50 creatives: `creative_id, format, hook_type, headline, primary_text, cta, launch_day`. 40 have been served. 10 have `launch_day` empty and have never been served. |
| `daily_reported.csv` | The 40 served creatives, one row per (day, creative) from each creative's launch day through day 60: `day, creative_id, budget, spend, impressions, clicks, purchases, revenue`. `budget` is what the buyer allocated to that creative that day; `spend` is what was delivered. One export, pulled at the end of day 60. |
| `daily_final.csv` | The same rows. A second export of the same table, pulled on day 67. |
| `competitor_launches.csv` | Competitor creatives we saw go live during the same period: `day, competitor, platform, format, hook_type, primary_text`. |

We will answer questions about the task and the protocol. We will not answer questions about how the data was generated. Everything you need to know about the dynamics is in the files.

## What we want

Not a spec. This is what would have to be true before we turn this on for a real account with real money.

**It decides.** Every morning, a split of $2,000 across 50 creatives, including the 10 that have never run.

**We believe it works.** Better than what a human does today, and we can see by how much, with an interval around it rather than a single number. How you earn that belief is your call. You can simulate a delivery environment and run policies inside it. You can replay the 60 days of history you already have. You can do both. We have an opinion about which is worth more here and we are not going to tell you.

**It has survived an attempt to prove it wrong.** For each thing your policy relies on, what result would have shown that it does not hold, and did you run that check?

**We trust the numbers it learns from.** You have two versions of the same 60 days. Whatever that means, the policy is going to learn from one of them.

**It handles a creative with no history.** Ten of them, and the only thing we know is what the creative says and what format it is.

**It is not surprised by the market.** We log competitor launches. Whether that is worth anything to a policy is for you to determine.

**It cannot do something stupid with $2,000.** A daily budget decision that runs unattended needs limits, and needs to behave when an input is missing or wrong, a day is skipped, or the same day gets run twice.

**A media buyer will look at it and either trust it or turn it off.** They will want to know why a creative got what it got, and they will want to overrule it.

You cannot build all of this in four hours. We are not asking you to.

## What to build

### 0. Pre-registration. First 30 minutes. Commit `PREREG.md` by 0:30.

Append-only. Revise it as often as you like by appending a timestamped entry; do not rewrite earlier entries. We read the sequence.

Before you start building, write down:

- **(a)** the objective you will optimize, and why that one;
- **(b)** each phenomenon you found in the data, one number or one plot per claim, with the figures or printouts committed alongside;
- **(c)** what you are going to build in the next three and a half hours, in order, and what you are consciously not building;
- **(d)** for each item in (c), the result that would make you drop it or change it.

We hold what you built against what you said here.

### 1. The one hard requirement

At 4:00 your repository contains a policy we can run. It may return a uniform
split and nothing else. It must run. Two ways to give us one, and we do not care
which you pick:

- **A process.** An executable `policy` at the repository root that speaks the
  JSON-lines protocol below. Any language.
- **A Python module.** `policy.py` at the repository root exposing class `Policy`,
  importable on Python 3.11 or later.

If both exist the executable wins.

- It must not read any ground-truth config you wrote and must not import a
  simulator you wrote. Utility code shared between the two is fine, and we will
  look at what you chose to share.
- When we evaluate it, the policy runs with no network access.
- Ten seconds per decision, ten minutes for a full thirty-day run.

Everything else below is what we want, not what we require.

### 2. Evidence that it works

A number with an interval, comparing your policy against both of these on the same footing:

- **B1 Uniform.** Equal share to all 50 creatives, every day.
- **B2 Windowed greedy with exploration.** Each day, for each creative, objective per dollar spent over the trailing 7 days. A creative with fewer than 2,000 trailing impressions, or zero trailing spend, takes the population mean rate. Top 5 by rate split 75% of the budget equally; everyone else splits the remaining 25% equally.

If you build a simulated environment, keep its ground-truth parameters in a config file rather than in code, and also run a **clairvoyant** that is allowed to read them. Report `(yours - B1) / (clairvoyant - B1)`. We compare that number across candidates, and an environment with no headroom in it shows up there.

If you replay history instead, say in the README what your number does and does not establish.

Whichever you choose, we will look for the checks that could have come out against you, and at what you did when one did.

### 3. Commands

One documented command that reproduces your headline number. One that runs your tests, if you wrote tests. We run both from a clean clone, on our machine.

### 4. README, 400 words or fewer

What you built first and why. What you skipped and why. What your evidence does and does not establish. What you would do with one more day. The commands.

## Repository

Layout, module boundaries, naming, tooling, language: all yours, including the policy itself. The only fixed thing is the protocol above, and only because something has to carry a decision from your code to ours.

Declare your dependencies in whatever standard manifest your stack uses. Tell us in the README how to install them if it is not obvious.

Two things we are measuring, so you can trade against them deliberately:

- We change things about the world and count how much of your repo would have to change with them.
- Structure too heavy for a four-hour project costs you the same as structure too thin. We are looking for a shape that fits the problem.

## Protocol

Thirty decisions, days 61 to 90, in order. Each one gives you every row recorded
before that day and asks for a budget split.

`history` rows are `{day, creative_id, budget, spend, impressions, clicks,
purchases, revenue}`, and the values are **as currently reported on the morning of
that day**. Rows for recent days may change in later calls. For days 61 onward,
`budget` is what your policy allocated: its share, after normalization, times the
daily budget (an allocation under $1 does not deliver and is recorded as 0).
Creatives never served have no rows. Shares must be at or above
zero; they are normalized for you. Creatives you leave out get nothing.

### As a process

We start your executable once and keep it alive for the whole run. One JSON
object per line, both directions. Flush after every line.

```
->  {"type":"init","creatives":[...],"daily_budget":2000.0,"seed":7}
<-  {"type":"ready"}
->  {"type":"allocate","day":61,"history":[...]}
<-  {"type":"allocation","shares":{"CR001":0.02,"CR002":0.05, ...}}
     ... twenty-nine more ...
->  {"type":"shutdown"}
```

`creatives` is the rows of `creatives.csv`, one object per row. Anything you write
to stderr is captured and shown to us; use it for logging.

### As a Python module

```python
class Policy:
    def __init__(self, creatives: list[dict], daily_budget: float, seed: int):
        """creatives: rows of creatives.csv, one dict per row."""

    def allocate(self, day: int, history: list[dict]) -> dict[str, float]:
        """Returns {creative_id: share}."""
```

Same data, same meaning, called thirty times on one instance.

## Rules

- Use any coding agent, model, editor or reference you like. Some tools are recorded more fully than others; see "Your tools".
- You work alone, at your own computer, at the time we agreed. Nobody watches, and nobody will contact you during the session.
- Run as many agents as you want. We are reading how you split the work, how you keep their outputs compatible, and whether anything lands in the repo that you did not read.
- Commit as you go. We read the sequence, not just the end state.
- **Ask us things.** Your agent has an `ask_panel` tool; from any terminal it is `./interview ask "your question"`. It answers anything about the task, the protocol, the harness or the rules, and refuses anything about the data or about what you should do. Refusals are normal and cost you nothing. Questions are logged with timestamps and we read them.
- **The session is recorded.** Recording started when you ran `./interview start`. It snapshots the working tree every 20 seconds into `.interview/snapshots.git`, which is separate from your own repository and leaves your own commit history alone. `./interview status` shows the time left. Every 30 minutes it uploads a checkpoint, a copy of the recording so far. When the time is up, packaging and upload happen by themselves, and they include whatever chat history your tools keep for this folder; `./interview collect` shows you in advance what that will be. Take breaks whenever you like, but the clock does not stop: keep the computer awake and online until `./interview status` says the session was submitted. Nothing here is hidden from you: all of it is Python in `.interview/`, and you are welcome to read it. Setup also writes small hook and panel files for each tool (in `.claude/`, `.codex/`, `.cursor/`, `.github/hooks/` and `.vscode/`, and `.mcp.json`); leave them in place. Do not disable any of it.

## Your tools

- **Recorded in full:** Claude Code and Codex. We read their chat history and their tool calls for this folder.
  - **Claude Code** (the CLI or the VS Code extension): start it in this folder, not a parent folder. Trust the folder when it asks, and when it asks about the `interview-panel` server from `.mcp.json`, allow it; that is the panel.
  - **Codex** (the CLI, the desktop app or the VS Code extension): run `./interview setup` first, then open Codex in this folder and trust the folder when it asks. Then approve the interview's hooks: type `/hooks` in the Codex CLI, or open Settings > Hooks in the desktop app, and trust the ones from this folder. Codex asks again whenever they change, so do this after setup. We have not yet checked the hooks in the VS Code extension: if you use the extension, also export each chat into this folder before the end. Work in this folder, not in a worktree: if the app makes one, its changes count only once they are in this folder. Do not use `codex exec --ephemeral`, and do not delete a Codex chat until the session is over: neither leaves a record.
- **Recorded, but also export:** GitHub Copilot in VS Code, and Cursor. We read their chat history and their tool calls for this folder too. As a backup, also export each chat into this folder before the end.
  - **Copilot in VS Code:** open this folder in VS Code and choose **Trust** when it asks; if you said no, run *Workspaces: Manage Workspace Trust*. Use the Chat view with **Local** or **Copilot** as the session target. To export a chat, run *Chat: Export Chat...* and save it in this folder. Inline chat (Cmd/Ctrl-I) is not saved: code it writes is allowed but, like inline completions, does not on its own show that a decision was yours. VS Code saves chats about once a minute: before you submit, wait a minute or close the window. If you let the time run out instead, its last minute may be missing, so export before then.
  - **Cursor** (the editor, 3.22 or later, or `cursor-agent` in a terminal): open this folder itself. When Cursor asks about the `interview-panel` MCP server, choose **Enable**; that is the panel. With `cursor-agent`, start it in this folder and trust the workspace. Run agents in this folder, not in a worktree or a best-of-N run: work there happens outside this folder. To redo a turn, send a new message: editing an earlier prompt, or restoring a checkpoint, deletes the later messages from Cursor's own record. To export a chat in the editor, open the chat's **...** menu, choose **Export Chat**, and put the file in this folder.
- **Check that your tool is recorded:** after your first prompt, `./interview status` shows hook records for your tool. If it still shows 0, its hooks are not approved or not running: go through its steps above again.
- **Any other agent, editor or chat:** allowed. Export the chat into this folder before the end, or record a share link to the session with `./interview link <url>`. While a tool we cannot read is in use, changes in this folder count as source unknown, including lines you type yourself: they are not credited to you.
- **Inline completions and inline chat** (editor suggestions, Copilot, Cursor Tab, Cmd/Ctrl-I): allowed. Code they may have written does not, on its own, show that a decision was yours. Where we cannot tell their lines from your typing, that holds for the lines you type too: once you use Copilot's chat or agent with inline suggestions on (the default), that is every line you type in the session, from its start; in Cursor's editor, it is when Cursor keeps no record of its completions. Say what you decided in a prompt or a commit message.
- **Cloud agents and web chats** (a Codex cloud task, a Copilot Cloud session, Cursor's cloud and background agents, a chat in a browser): their record stays on the provider's servers. Record each session as you go with `./interview link <url> [note]`, using a share link that anyone with it can open (Share, then copy link). A private chat URL shows us nothing. To correct a link, record it again; do not edit the files in `.interview/`.
- **Findable in the record:** every result you report must be findable in the record. What we cannot find there counts as unverified.

## How we judge

Nobody finishes. We are not scoring volume. We would rather see three things built correctly and verified than seven built and unverified.

- Does what you built match what you said you would build, and did you change course when your own evidence told you to?
- Can your policy see anything it should not?
- Which of your choices did the agent make for you, and did you check them?
- Does your policy survive in a world you did not build?
- Does your repo run on a machine that is not yours, and do your tests fail when they should?
- Is there room between your policy and the clairvoyant, and can you say what lives in that gap?
- If you shipped a signal, did you check that it carries information before trusting it?
- Of everything you did not finish, would we have cut the same things?
