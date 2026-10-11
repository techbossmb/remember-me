# Long-term memory (remember.me)

Memory for the current project lives in plain files under `~/.remember`, shared with any other assistant the user
uses. It is available through the `remember_*` tools.

- At the start of work, `remember_overview` shows every thread, the standing rules and open items. Follow the rules.
- To answer from history, pick the threads that matter by meaning and open them together with `remember_zoom`
  (ids + the question). `remember_spans` gives verbatim text; `remember_grep` searches the log. The log is the record.
- `remember_status` says what is stored and what is not mapped yet. If memory has not been started for this project,
  the user can start it from what is already in this conversation: write the conversation so far with `remember_log`
  (user messages verbatim, `source: "recalled"`), say whether this conversation began before what you can see, then
  build the map by following `remember_builder_prompt` (`remember_chunks`, `remember_chunk`, `remember_apply_ops`).
