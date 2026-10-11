# Long-term memory (remember.me)

remember.me keeps a project's conversations (verbatim) and a map of them, on the user's computer. Everything goes
through the remember_* tools; they run where the memory lives, whatever assistant or app you are in. Pass `project`
(the name of what the user is working on; remember_projects lists existing ones; ask once if unclear).

Using memory
- At the start of work, read remember_overview: every thread, the standing rules, open items. Follow the rules.
- To answer from history, pick the threads that matter by meaning and open them together with remember_zoom (ids +
  the question); remember_zoom with recent=5 for "where were we". remember_spans gives verbatim text, remember_grep
  searches, remember_window covers a time range. The log is the record: if it disagrees with the map, say so and go
  with the log.

Starting memory (when the user asks, or accepts the offer)
1. remember_status says what exists. If Claude Code was used for this folder, remember_ingest_sessions brings those
   sessions in.
2. Save what is in your context with remember_log: the conversation so far, user messages verbatim, your replies as
   written (long tool output summarised in a line), source "recalled".
3. Say plainly what is missing: if your context starts with a summary of earlier turns, the verbatim start is gone.
   A claude.ai data export can be imported selectively with remember_import (it lists conversations; the user picks).
4. Build the map (see "Updating the map"), then show it: remember_show_map returns a link for the user to click.

Saving as you go
- Save new turns with remember_log at natural pauses (after a task is done, before a long break, when the user says
  "remember this"). Repeating is safe; already-saved messages are skipped. Some environments also save
  automatically; saving anyway does no harm.

Updating the map
- When remember_status or the overview says messages are not in the map yet, update it after finishing what the user
  asked, out of the way of the conversation: hand it to a sub-agent if you can start one, otherwise do it briefly.
  Follow remember_builder_prompt: remember_chunks(new_only=true), then for each chunk remember_chunk and
  remember_apply_ops, then remember_coverage and remember_validate. Don't mention it unless asked.

The map viewer
- remember_show_map: start or reuse the viewer and give the user the link. remember_viewer: start, stop (works
  wherever it was started), restart (if the link doesn't load), status, autostart_on/off (start at login; offer once).

Never ask the user to run commands; do it through the tools.
