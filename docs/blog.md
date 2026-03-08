# FDA: The Honest Story of Building an AI That Doesn't Forget

*March 2026*

---

I run a small software consultancy in Korea. One client, a beauty reseller on Oliveyoung, has an Azure VM with Airflow DAGs, a Django backend, and a Postgres database. I manage their brand-level sales emails, inventory checks, and masterfile syncs. The work involves jumping between KakaoTalk (where the client messages me), SSH sessions into their VM, and my local dev environment.

I use Claude every day. It's incredible. But every morning I open a new chat and re-explain the same architecture, the same file structure, the same business context. Claude doesn't know I deployed a hotfix at 2 AM. It doesn't know what my client asked me on KakaoTalk yesterday. It starts fresh every time.

FDA started because I got tired of that.

## Day 1: A Scaffold and Nine Bugs (Feb 9)

I had Claude generate the initial scaffold — 22 Python modules, 1,744 lines. FDA Agent, Executor Agent, Librarian Agent. A journal system. A web setup UI. Telegram and Discord bots. On paper, it looked complete.

Then I tried to run it.

Nine commits on day one. Fix openpyxl version (3.9.0 doesn't exist). Fix test connection returning HTML instead of JSON. Fix SQLite threading error. Wire up the ask command to actually use the Claude API. The scaffold was beautiful documentation wrapped around code that didn't work.

I kept going.

## The Identity Crisis (Feb 9–17)

The first real struggle wasn't technical — it was figuring out what FDA should be.

Commit `6b7f092`: "Update FDA agent to be a personal assistant, not project management tool." I thought it should manage my whole life — calendar, tasks, everything.

A week later, commit `2b78206`: "Redesign FDA for Datacore client automation pipeline." I threw out the generic assistant idea and rebuilt it around my actual work — KakaoTalk message intake, per-client YAML configs, SSH to Azure VMs, approval workflows through Telegram.

This was the first lesson: **don't build a general-purpose AI system. Build for the specific pain you feel every day.**

## Discord Voice: The Model That Doesn't Exist (Feb 19)

I wanted to talk to FDA through Discord voice. The idea was simple: join a voice channel, say "hello FDA", get a spoken response back.

The reality took hours.

First, I configured the OpenAI Realtime API with `gpt-5-mini` — the text model. Not a voice model. Claude corrected me to `gpt-realtime-mini`. Then the `websockets` package wasn't installed, so the voice connection would complete at the network level and immediately fail silently. Then the bot wouldn't respond to speech — wrong model name again. Then py-cord crashed with "list index out of range" during the WebSocket handshake, which turned out to be a race condition in the library itself.

I eventually got voice working, but this was my introduction to the gap between "the code compiles" and "the feature works." Every integration layer had its own failure mode, and they stacked.

## The Connectivity Graveyard (Feb 19–22)

Getting three chat bots to work simultaneously was harder than building any one of them.

**Telegram** crashed immediately: `set_wakeup_fd only works in main thread of the main interpreter`. The bot library assumed it was running in the main thread, but the orchestrator ran it in a daemon thread. A fundamental threading conflict.

**Slack** had a different problem — it would answer exactly one message, then go silent forever. The bug was a thread-safety issue: instance-level variables for the reply context got overwritten when a second message arrived. The first reply worked. Every reply after that wrote to a stale reference.

**Slack Socket Mode** silently dropped WebSocket connections. The built-in handler would connect, work for a few minutes, then quietly die. I had to switch to a websockets-based handler to get reliability.

**Discord text mode** had no agentic tool loop at all. It could only respond to voice, not text commands.

And the bots were siloed. I asked Telegram about the worker agent: "I'm not aware of any other agents." I asked Discord about a task: it could see the task but couldn't dispatch anything. Each bot was built with a different architecture and different tool sets. They shared a brain but not a body.

One by one, I unified them. Same tool definitions. Same orchestrator routing. Same approval workflow. This took three days of "slack is not responding after restart" and "telegram says it's not aware of any other agents."

## The File Routing Problem (Feb 21–22)

The remote worker cached 2,000 files from the client's VM. When I asked about the brand-level sales email feature — the most important thing the client uses — it couldn't find a single relevant file.

The issue was simple: the config pointed at `~/dev/main/manage`, but the email DAGs lived in `~/airflow`. The worker was scanning the wrong directory.

I added multi-path scanning. Then the file identification was too restrictive — Claude was only picking 2 of 11 relevant DAGs. I raised limits, added plural/singular keyword variants, told Claude to "be INCLUSIVE." The heuristic fallback was returning junk: `.log` files, `attempt=1.log`, `.bak` files.

Smart file ranking was born out of this mess: a 4-signal importance score (recency, complexity, import hub score, query history) that tried to surface the right files before Claude ever had to guess.

## The Breakthrough: "Why Is It So Dumb?" (Feb 24)

This was the pivotal moment.

I had Claude Code installed on the client's Linux VM. I also had FDA's remote worker agent pointed at the same VM. I asked both the same question about the Airflow DAGs.

Claude Code gave a thorough, accurate analysis. FDA's remote worker gave garbage.

Same model. Same codebase. Completely different results.

I asked: **"I'm just very surprised how dumber remote agent is compared to CLI Claude Code on Linux. Why is the gap happening and how can we close the gap?"**

The answer was architectural:

**Claude Code iterates.** It reads a file, realizes it needs another file, greps for something, reads more — 10 to 15 tool calls in one conversation. My remote worker got ONE shot: identify files, read them, answer. If it picked the wrong files, there was no recovery.

On top of that, the Slack bot's tool loop was broken — an investigation flag was never being set, so it would loop 5 times calling `remote_task` instead of giving the worker time to work. The JSON parsing was fragile — 4 out of 5 file identification calls failed silently. And the worker had no shell access — it couldn't run `ls`, `grep`, or `airflow dags list`.

This was the intelligence gap. **Not the model. The architecture.**

I added `run_remote_command` so the Slack bot could execute shell commands directly via SSH. Fixed the JSON parsing with regex fallback. Set the investigation flag properly. Raised tool iterations from 5 to 10, then added a graceful summary when the limit was hit instead of discarding everything.

A week later, I asked the deeper question: "Are our models even agentic? An agentic model wouldn't call 'searching for file directories' every time it's queried." The agents were still running the full 2,000-file scan pipeline for every question — even a simple `ls` command.

So I rebuilt both workers from scratch. Replaced the rigid 4-step pipeline (scan all files → Claude picks relevant → read → generate fix) with a proper tool-use loop where Claude autonomously decides what to explore. Five tools: `list_directory`, `read_file`, `search_files`, `write_file`, `run_command`. One call to `complete_with_tools()` instead of three separate Claude calls.

**What I expected from FDA was something similar to Claude on mobile, but better, with deeper context understanding.** The engineering had to be even-or-superior to vanilla Claude Code. That became the north star.

## What I Learned

**1. Start with your specific pain, not a grand vision.**
FDA went through three identity changes before it clicked. The "personal assistant" version was too vague. The "Datacore automation" version was too narrow. The right answer was somewhere in between: a persistent AI team member that remembers your work and meets you on every platform.

**2. Integration is harder than implementation.**
Building a Telegram bot takes an afternoon. Making it work simultaneously with Slack and Discord, sharing state, routing to workers, handling approvals — that's the real work. Every platform has its own threading model, its own failure modes, its own way of silently dying.

**3. The intelligence gap is architectural, not model-level.**
The same Claude model can be brilliant or useless depending on how you wire it. Give it one shot and it guesses. Give it a tool loop and it explores. The difference between a dumb bot and a smart agent is whether it can iterate.

**4. Your AI system needs to be at least as good as the vanilla tool.**
If your custom agent is worse than just opening Claude in a browser, nobody will use it — including you. I spent weeks closing the gap between "custom agent" and "vanilla Claude Code" before FDA became something I actually preferred to use.

## Where It Is Now

FDA runs 24/7 on my machine. It posts morning briefings at 9 AM. It summarizes conversations at 9 PM. It monitors its own health and restarts crashed bots. I ask it questions from Telegram on my phone, approve code changes from Slack on my laptop, and review daily notes on Discord.

It remembers what happened yesterday. It knows the client's architecture. It can SSH into the staging server and actually look at the code before answering.

It's not perfect — I'm still adding features, still fixing bugs, still finding new ways the bots can silently die. But it's mine, it's persistent, and it doesn't start fresh every morning.

That's the difference.

---

```bash
git clone https://github.com/white-dots/FDA.git
cd FDA
pip install -e ".[all]"
fda onboard
```

[GitHub](https://github.com/white-dots/FDA) — Built by Jae Heuk Jung, powered by Claude.
