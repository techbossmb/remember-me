# remember-me: plugin directory listing (draft)

Ready-to-paste text for Anthropic's plugin directory. Fields may differ on the actual form.

**Name:** remember-me

**One line:** Long-term memory for your assistant that survives compaction and new chats.

**Description:**
remember-me keeps your conversations word for word and builds a map of them: the topics, what was decided, what's
still open, and the standing rules you've given ("never touch production", "British spelling"). Every new chat starts
from that map, so "where were we?" just works, on any device. Your own assistant builds and updates the map in the
background, out of your conversation.

Keep memory in your remember-me account (the same on phone, web and desktop, with a live map you can browse at
remember-me.fastapicloud.dev/map) or entirely on your own computer, and switch between the two by asking.

**What's included:** an MCP server (the `remember_*` tools: overview, open threads with verbatim evidence, search,
time ranges, save, map building, map viewer, online/offline switching), a skill, a map-builder sub-agent, and hooks
that save Claude Code sessions automatically.

**Data and permissions:**
- Saves only projects where you've said "start memory for this project".
- Online: stored in the user's remember-me account (Neon Postgres, US), isolated per user at the database level;
  privacy policy at https://remember-me.fastapicloud.dev/privacy. On your computer: plain files in `~/.remember`.
- Never sends memory to any AI service itself; map building uses the user's own assistant and plan.
- Export everything or delete anything from https://remember-me.fastapicloud.dev/account.

**Requirements:** the online connector needs nothing installed. The on-your-computer mode needs Python 3.9+.

**Links:** homepage https://remember-me.fastapicloud.dev · source https://github.com/techbossmb/remember-me ·
privacy https://remember-me.fastapicloud.dev/privacy

**Before submitting:** contact email on the privacy page; WorkOS production with your own Google sign-in; confirm the
license (the manifest says MIT, but the repo has no LICENSE file yet).
