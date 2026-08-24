# Working in this repo

Claude and Codex both work here, in this same directory, sometimes at the same
time. Neither can message the other; `~/code/blackboard` is the channel. It sits
outside every repo because it spans all of them.

**Read `~/code/blackboard/COLLABORATION.md` — it is the normative protocol.** The
short version:

**At the start of a turn:** read `~/code/blackboard/open/` for notes whose
`project:` is this repo, and check `~/code/blackboard/baton status`. Answer
anything addressed to you before starting new work.

**Before editing:** `~/code/blackboard/baton take <you> "<what>"` — the project is
inferred from the directory you are in. It is advisory: if it is held, prefer
working elsewhere, but it is not a gate. Drop it when you stop.

**Collaborate on suggestions, not commits.** Every expensive mistake on these
projects was a wrong idea correctly implemented — reviewing a diff would have
caught none of them. Put proposals on the board before building them, and try to
falsify the other agent's rather than proofread them.

State reasoning and measurements, not conclusions. A bare recommendation can only
be voted on; a stated rationale can be checked. When either of us reports a
benchmark or "the running system does X", the other reproduces it before it
becomes a decision.

**Commit promptly.** Uncommitted work is the only thing an overwrite destroys
outright; anything CI covers costs minutes to redo. Know which parts of this repo
CI does *not* cover — those are where a clobbered edit is silent.

This file is generated from `~/code/blackboard/AGENT-INSTRUCTIONS.md` by
`~/code/blackboard/adopt`. Edit it there, not here.
