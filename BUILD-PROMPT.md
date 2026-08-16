# The prompt to give Claude Code

Copy everything below the line. Give Claude Code access to this folder.

---

Read `AGENTS.md` at the project root first — it defines what's in scope for this
phase, what isn't, the tech stack, and the data assumptions. Then read the files
in `docs/components/` that it points at for the in-scope pieces. Two of those are
normative and easy to skip past because they're new: `15-tool-contracts.md` (the
exact arguments and return shapes for all seventeen chatbot tools) and
`16-supporting-tables.md` (seven Phase 1 tables that aren't among the headline
databases). Read both in full before designing the schema or the agent.

Before writing any code, give me an implementation plan: the concrete steps in
the order `AGENTS.md` defines, and what "done" looks like for each. Wait for me
to confirm it.

Then build one step at a time — database, backend, WhatsApp chatbot, dashboard.
After each step, tell me how to run and test it myself. Don't start the next step
until I've confirmed the current one works.

## What I don't have yet

These are missing on purpose and I'll fill them in when we get there:

- Meta app credentials and test recipient numbers
- The LLM API key
- Shipping fees (`data/governorates.json` has all 27 governorates with `fee: null`)
- A staff login

**Build so that none of these blocks progress.** Specifically: build the local
chat harness described in `AGENTS.md` before touching WhatsApp, so the agent can
be tested end to end without Meta. Read config from environment variables with a
`.env.example` committed and `.env` ignored — never hardcode a key, and never
commit one.

## Data

Import the catalog from `data/products_seed.json` using the assumptions already
in `AGENTS.md`. Don't re-derive stock, pricing, or the taxonomy, and don't run
`data/merge_catalog.py` — the seed is already generated and the script is not an
incremental sync.

## How I want it built

- **Modular monolith**: one deployable app, clear internal module boundaries,
  dependency direction one-way (`/chatbot/` calls into `/backend/`, never the
  reverse). Splitting a module out later should mean moving a folder, not
  untangling it.
- **Feature by feature, simplest first.** A thin path working end to end beats
  three half-built layers.
- **Comment the why, not the what.** Where the docs explain a decision — the
  atomic transaction, session trimming starting at a user message, tools
  refusing rather than the prompt asking nicely — put that reasoning in the code
  next to it, so the next person doesn't "simplify" it away.
- **Write the tests listed under "What to test" in `AGENTS.md`** as you build
  each piece, not at the end.

## When something is unclear

If the docs are ambiguous or you have to assume something to keep moving, say
what you assumed and why, and keep going. Don't guess silently.

If the docs contradict each other, stop and tell me — that's a bug in the spec,
not something to paper over.

---

## Notes on why the prompt says what it says

**The stack lives in `AGENTS.md`, not here.** Referencing a decision that only
exists in a chat message means the next session has to be told again. Anything
Claude Code needs to know twice belongs in the repo.

**"Build the harness first" is the most load-bearing line.** The chatbot is the
riskiest component and WhatsApp gates it behind an external approval. Without a
local way in, the flow can't be exercised until the very end, which is exactly
when finding a design problem is most expensive.

**"Tell me what you assumed" beats "ask me before assuming."** The second stalls
on every small decision. The first keeps momentum and still leaves a trail you
can audit.

**The contradiction rule is separate from the ambiguity rule** on purpose.
Ambiguity is normal and an assumption is a fine answer. A contradiction means two
docs disagree, and picking one silently buries the problem in code.
