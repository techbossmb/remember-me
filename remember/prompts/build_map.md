# remember.me — map builder spec (v1: ordered replay)

`remember …` below means however you run the remember.me CLI (`python3 <install dir>/remember/cli.py …`), or the matching `remember_*` MCP tool.

You maintain a project's **memory map**: a small, information-dense graph that a future assistant reads at the start of every session (and after every compaction) to know what happened, what was decided, what is open, and how this person wants work done. When the map isn't enough, that assistant follows span pointers back to the verbatim log.

The work can be anything: software, research, writing, planning, design. Don't assume coding.

## The one rule that matters most: replay in order

The map is built by **replaying the conversation in the order it happened**, one chunk at a time. Never read everything first and write the map from the whole picture. What is true *now* depends on what was said *last*:
- "never commit" followed later by "you can commit to claude-staging now" means the second **supersedes** the first.
- A question asked early and answered later is **resolved**.
- A plan that a later message changed has its old version **superseded**.

So, for each chunk:
1. Look at the current map (`remember overview --ids`: rule ids, node ids, claim ids).
2. Read the chunk (`remember chunk N`). Its messages are already in time order.
3. For each message that matters, **correlate it with what's already there**:
   - Does it touch an existing **rule**? Check every active rule; there are few. If so, decide:
     - **supersedes**: `rule.supersede` with the new text;
     - **narrows**: supersede with the narrower text;
     - **extends**: add a rule;
     - **contradicts** without clearly replacing: add a `contradicts` edge or an open question.
   - Does it touch an existing **thread**? Update its summary to the new current state (`node.update`). Add claims, and supersede claims it replaces (`claim.supersede`; for an answered open question use `new` with the decision).
   - Is it a **new topic**? Create a node. Anything that could be closed on its own is its own node. Something discovered while working on X that could outlive X is a **sibling** with a `discovered_during` edge, not a child.
   - Is it the user **restructuring the map** ("merge these", "that's settled", "split that out")? Apply it (`node.merge`, `node.update` status, `node.move`).
4. Apply the ops with `remember ops <file.json>`. **Every op carries `src`**, the exact message it comes from (e.g. `myproject/session.md#m0012`, as printed by `remember chunk`). The tool records *when* it was said from `src`; that's how ordering and supersession work. Check the warnings it prints.
5. **Coverage check:** every substantive message in the chunk must be reachable from the map (a node's spans, or a claim's/rule's spans). Messages that matter but fit no claim still get added to the right node's `spans` via `node.update`. Run `remember coverage` at the end; the goal is close to 100%. Acks and small talk don't count.
6. Next chunk.

An older message can never override something stated later. The tool refuses it and warns you; that's expected when sources overlap.

**Cross-project statements:** if a message changes something in another project's map (e.g. a permission for a different project), add `"project": "<that project>"` to the op and look up its ids with `remember overview --ids --project <that project>`.

## Inputs
- `remember chunks` (or `--new` for only what isn't mapped yet): the replay plan. `remember chunk N` prints a chunk.
- Messages with role `summary` are a harness's summary of earlier turns whose verbatim text isn't available; use their facts, and say in the project summary that earlier history is summarised.
- Sessions marked `recalled` were written from an assistant's context, not copied from a transcript; treat exact wording in them with some caution.
- Messages with role `pivot` are the user's own notes marking a digression. They're strong evidence of a topic boundary.
- Messages with role `compaction`, or text starting "This session is being continued from a previous conversation", are harness-written summaries. Use their facts; their timestamps are when the summary was written, not when the facts were stated.
- Overlapping sources (e.g. a user's hand-copied paste and recovered fragments of the same conversation) will show the same exchange twice. Don't duplicate nodes or claims; add the second `src` to the existing node with `node.update`.

## What makes a good map

**Nodes**
- One `project` root (`type: project`), then `thread` nodes nested with `parent` (part-of only).
- Aim for 8–25 threads. `title` ≤ 8 words.
- `summary` ≤ 60 words, describing the **current state**, not the story. Rewrite it whenever the state changes.
- `status`: active, paused (say where it stopped), resolved, or reference.
- `aliases`: every name used or likely to be used, **especially the user's informal names** ("the fastapi thing", "the DLM thing"), and how someone would describe the problem it solves ("data loss", "disk failure" for backups).
- A summary describes the state **as of its own `src` message**. Never write later facts into an op whose `src` is earlier (e.g. "capacity measured later" on a Sep 18 message); add a new op at the later message instead. The map is cut by time, and a back-dated summary leaks the future.
- Every **status change the user reports** ("I already ran the migration", "I set that up", "she reconnected") becomes a claim, even inside a covered message. A claim like "not run by agent" will otherwise be read as "not run".
- **Playbooks** (`type: playbook`): a multi-step working method (how to preview UI changes, how to verify a draft, how to run a literature search). Put the full ordered steps in `requirement` claims, copied concretely. Then add a short rule that points to it, with `"playbook": "<node id>"` and a `"when"` trigger that names the situation where it applies ("when changing anything in the UI", "when you need information from the AWS box"). Without a trigger, agents open the playbook for every question. One-line rules lose the steps that matter. When a later message applies the same method with extra steps (e.g. how a change was verified: type-check, lint baseline, testing every input path), add those steps to the playbook too; don't leave them on the feature's node.

**Lists** (`type: list`): anything the user (or assistant) asks to keep track of, e.g. "things to fix", "questions for the vendor", "ideas for chapter 3". Each entry is an `item` claim. Done or dropped entries are superseded with a reason, never deleted.
- Also catch **items said in passing** ("we should fix X later", "remind me to…", "parking that for now") and add them to the project's open list. These are what gets silently lost.

**Claims**: kinds `decision`, `requirement`, `rejected` (with `why`), `open`, `item`.
- Decisions carry a short **verbatim `quote`** of the user when they stated it.
- **Keep concrete facts verbatim**: service names, numbers, flags, file paths, prices. Write "db, backend, frontend, adminer, traefik", never "app, traefik, postgres".
- **Negative knowledge**: if a topic was raised but never actually discussed, say so in the summary.

**Edges**: read `from <type> to` as a sentence ("T14 pivoted_from T10" = T14 came from T10). Types:
- `pivoted_from`
- `revisits`
- `discovered_during`
- `depends_on`
- `supersedes`
- `contradicts`
- `related`

Add a `note` when the reason isn't obvious.

**Rules** (procedural memory): standing instructions, permissions, preferences and agreed working methods that should apply every session without being asked.
- Include methods the user had to push for, even if never phrased as a rule.
- `scope`: `user` if it plainly applies across all their work, else `project`.
- Keep them short and imperative; 5–20 per project.
- A new permission covers exactly what was granted, nothing adjacent.

**Never invent.** Every claim and rule traces to its `src`. If unsure, make it an open question or leave it out.

## Op reference (JSON list for `remember ops`)
```json
[{"op":"node.create","id":"T3","parent":"T0","title":"...","status":"active","summary":"...","aliases":["..."],"src":"f.md#m0004"},
 {"op":"claim.add","node":"T3","kind":"decision","cid":"T3.d1","text":"...","quote":"...","src":"f.md#m0007"},
 {"op":"claim.supersede","id":"T3.d1","reason":"changed by user","new":{"kind":"decision","cid":"T3.d2","text":"..."},"src":"f.md#m0031"},
 {"op":"rule.add","rid":"R3","scope":"project","text":"UI work: follow the preview playbook","playbook":"T9","when":"changing anything in the UI","src":"f.md#m0005"},
 {"op":"rule.update","rid":"R3","when":"changing anything in the UI","src":"f.md#m0005"},
 {"op":"rule.supersede","rid":"R2","reason":"user granted","new":{"rid":"R7","text":"..."},"src":"f.md#m0040"},
 {"op":"edge.add","from":"T5","to":"T3","type":"discovered_during","src":"f.md#m0012"},
 {"op":"node.update","id":"T3","status":"resolved","summary":"...","src":"f.md#m0050"}]
```
Use readable ids you choose: nodes `T<n>`, claims `<node>.<letter><n>`, rules `R<n>`. Stay consistent across chunks.

**Never shorten stored rules or claims to fit the render budget.** The render compresses what's shown; the store keeps full wording. If the render overflows, tighten node *summaries* only, and never drop a rule's meaning.

When done: run `remember validate` and fix real issues; warnings about older statements are fine. Then run `remember overview` and check that it isn't truncated.
