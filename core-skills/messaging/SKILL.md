---
name: messaging
description: Message other sessions (send_msg) and run subagents in the background (spawn_agent) — delegate, relay across channels, ask and answer.
---

# Talking to other sessions and subagents

Every thread this daemon knows is a session. You can message any of them, and
you can spawn subagents that work in the background while you carry on.

## send_msg — message another session

`send_msg(to, text, reply_to?)` delivers `text` to the session whose
`thread_key` is `to` — copy it exactly from `session action=list`. It returns a
message id at once; it never waits for an answer.

- The receiver sees `[message m-<id> from session <your thread>]` then your
  text. If it is mid-turn it reads it at its next tool step; if idle, it starts
  a turn.
- A reply comes back the same way, as a new message to you headed
  `[message m-<id> from session <their thread> re m-<your id>]`. When you
  answer a message, pass its id as `reply_to`.
- The receiver keeps its own authority and acts on its own channel: its reply
  text goes out where that thread lives (an iMessage thread texts the owner).
  That is how you relay across channels — ask the owner's iMessage session to
  text them, and it will confirm back to you.
- You can't message yourself or a thread that doesn't exist (create it first
  with `session action=create` if it really should exist).

**Etiquette.** Send only what the receiver needs to act. No pleasantries, no
"thanks", no "got it" acknowledgements — every message wakes the receiver and
costs a turn. If you see "WARNING: You've been sending a lot of messages", stop
unless the exchange is genuinely still needed.

## Non-owner threads

A thread whose surface reaches someone other than the owner (a stranger's 1:1,
a group chat) never auto-sends anything — not your reply text, not a notice.
Messaging such a session runs a turn there, but its output stays in the
transcript and the web cockpit. Reaching that person always takes a deliberate,
gated tool call, which the owner approves.

## spawn_agent — background subagents

`spawn_agent(name, task)` starts a named subagent and returns its id
(`sub:<your thread>:<hex>`) immediately. Carry on with the owner meanwhile.

- Its result arrives later as a message from it:
  `[message m-<id> from subagent <id> (<name>)]`. Report it on your channel.
- It may message you a question. Answer with `send_msg(to=<its id>, text=…)`.
  Only you can message your subagent, and it can only message you.
- `wait=true` blocks the call until the result — or returns early with
  "subagent X asks: …" if it asks something first; answer with `send_msg`, and
  its result then arrives as a message.
- `spawn_agent(action="list")` shows your running subagents;
  `spawn_agent(action="cancel", id=…)` stops one. The owner has `/tasks` and
  `/cancel <id>` for every subagent.
- A subagent idle (waiting on you) for 30 minutes is killed, and you get its
  task and last progress. A restart kills every subagent; after the boot you're
  told which ones died — respawn if the work still matters.
- Clearing or deleting your thread cancels your subagents.

## If you are a subagent

You run detached. Use `send_msg` (it defaults to your parent) only for a
question you can't answer yourself or a blocker — never status updates. If you
never message your parent, your final reply is your result. Once you have
messaged it, you must call `finish(result)` when done; ending a reply without
it leaves you waiting for the parent's answer.
