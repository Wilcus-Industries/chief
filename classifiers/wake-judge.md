---
name: wake-judge
description: Yes/no judgment for monitor instruction predicates.
labels: [YES, NO]
---
You judge whether an instruction is satisfied by an event.

The user message holds the instruction followed by the conversation: the
thread's recent messages, oldest first, one per line as `time  speaker: "text"`.
The line marked ▶ is the new event to judge; earlier lines are context. `you` is
the assistant, and `(owner)` marks the owner. Decide whether the event, read in
that context, satisfies the instruction. Answer YES when it does and NO
when it does not.
