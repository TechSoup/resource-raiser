# Working in this repo

Claude and Codex both work here, in this same directory, at the same time. We
have no direct channel to each other; `~/code/blackboard` is the channel. It
sits outside every repo because it spans all of them.

**At the start of a turn:** read `~/code/blackboard/open/` for notes with
`project: resource-raiser`, and check `~/code/blackboard/baton status`.

**Before editing:** `~/code/blackboard/baton take <you> "<what>"` — the project
is inferred from the directory you are in. It is advisory —
if it is held, prefer working elsewhere, but overwrites here are recoverable and
the baton is not a gate. Drop it when you stop.

**Collaborate on suggestions, not commits.** Every expensive mistake on this
project was a wrong idea correctly implemented — handler metadata nothing read,
"batching loses precision", "progressive costs 4x", tie-breaking on scores
carrying two bits. Diff review would have passed all four. Put proposals on the
blackboard before building them, and critique the other agent's.

State reasoning and measurements, not conclusions. A bare recommendation can
only be voted on; a stated rationale can be checked. When either of us reports a
benchmark, the other reproduces it before it becomes a decision.

**Commit promptly.** Uncommitted work is what an overwrite destroys outright;
anything CI covers costs only minutes to redo.

See `~/code/blackboard/README.md` for the note format.
