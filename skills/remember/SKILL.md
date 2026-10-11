---
name: remember
description: Long-term memory for this project (remember.me). Use when the user wants to start memory here, asks what was decided or where work stopped ("where were we", "what did we decide about X"), wants to see their memory map, or wants to import chat history.
---

# remember.me

Use the `remember_*` tools for everything. They run where the memory lives (the user's computer), whatever
environment you are in. Don't run the memory scripts with a shell: depending on the app, your shell may be a
separate sandbox that can't see the user's memory.

Read `remember_guide` once and follow it. It covers using memory, starting it, saving as you go, updating the map,
importing history, and the map viewer.

In Claude Code and Cowork specifically:
- To update the map, hand the work to the `remember-me:builder` agent with the project name, so it stays out of the
  user's conversation.
- Some sessions are saved automatically, but not all. Saving at natural pauses with `remember_log` is always safe,
  because repeats are skipped.
