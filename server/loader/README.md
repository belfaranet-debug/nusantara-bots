# Skill Loader + Character Format (Phase 2)

Zero-dependency, Python standard library only. No network, no model
API calls, no database import step: the loader reads plain folders
and files, validates them, and reports every item with its status.
Nothing valid-or-invalid is silently dropped.

## Why Python stdlib (choice, documented)

Phase 1's memory service is Python + SQLite stdlib. Sharing that
runtime means one interpreter, no `npm install`, no lockfile, and
nothing to download on a self-hosted server. That is load-bearing
for the ownable/sellable bar of this build: a customer installs a
pack on their own server, with their own data, and it loads with no
provider, no registry, and no network at install time. A character
or skill pack is files, not a subscription — it can be sold, moved,
and re-hosted because nothing in it phones home.

## Paths (contracts for later phases)

| What | Path (absolute) | Rule |
|---|---|---|
| Runtime character directory | `server/characters/` | **App shell and crew installer MUST use exactly this directory.** One `.md` file per character. |
| Skill shelf (read-only) | `~/workspace/grokbot/skill-pack/` | This loader NEVER writes here. |
| This loader | `server/loader/` | Importable as package `loader` from `server/`. |

In code, the same paths are `loader.CHARACTERS_DIR` and
`loader.SKILL_SHELF_DIR`.

## Skill format

One folder per skill, containing `SKILL.md`:

```markdown
---
name: my-skill
description: One line, what it does and when to use it.
version: "1.0.0"
---

# My Skill
...body: instructions, steps, rules...
```

Rules:

- `name` MUST equal the folder name (else `name_mismatch`).
- `description` MUST be present and ≤ 1024 chars (else
  `description_too_long` / `missing_description`).
- `version` MUST be present and non-empty, at the top level
  (`version:`) OR under `metadata:` (`metadata.version:`). The real
  shelf uses `metadata.version`; both are accepted and the loader
  records which one in `version_source`.
- Body after the frontmatter MUST be non-empty.
- A folder without `SKILL.md` is invalid (`missing_skill_md`). In
  particular a **crew pack** (README + `characters/`, no `SKILL.md`)
  is not a skill and fails as a skill — the crew installer (later
  phase) reads crew packs; this loader does not pretend they are
  skills and does not convert them in place.

## Character format — an employee, not a chat prompt

One markdown file per character in `characters/`, frontmatter +
prompt body:

```markdown
---
name: YT Strategist
role: Channel strategy, positioning, and content calendar
skills:
  - team-memory
  - bot-live-presence
tools:
  - read_file
  - write_file
memory_scope: private+team
---

You are the Strategist on the channel team. You report to the
Chief Officer. ... (persona prompt: job, inputs, rules, output format)
```

Fields (all required):

- `name` — display name. NOT required to equal the file name: the
  file name is a slug (`yt-strategist.md`), the name is human-facing.
- `role` — one line: what this character owns.
- `skills` — list of skill names (inline `[a, b]` or block `- a`
  form). May be empty (`skills: []`), but must be a list.
- `tools` — list of tool names the character may use, same forms.
  May be empty, but must be a list.
- `memory_scope` — exactly one of `private`, `team`,
  `private+team` (see `../MEMORY.md`: private = own notes only;
  team = shared team memory; `private+team` = both).

Why this makes a character an employee rather than a chat prompt:
a prompt only says how to talk. This file declares what the
character can do (`skills`), what it may touch (`tools`), and what
it remembers (`memory_scope`) — and the runtime binds exactly those
declarations. A character cannot silently gain a skill, a tool, or a
memory scope its file does not declare, and its memory survives
across sessions instead of resetting every chat. That declaration
is also what makes a character sellable as part of a pack: buyer
gets the file, the file is the employee's job description, and it
runs on the buyer's own server under their own API key.

Legacy note: the `crew-youtube-team` pack's `characters/*.md` use
a `# Name` / `Role: ...` / `---` header, NOT frontmatter. They fail
this format with `missing_frontmatter` — reported as-is. Do NOT
convert them in place; a later crew-installer phase may offer an
explicit, owner-approved import that writes NEW files in this
format into `characters/`.

## Import paths and function signatures

Run from `server/` so `loader` is importable:

```python
from loader import load_skills, load_skill, load_characters, load_character
from loader import CHARACTERS_DIR, SKILL_SHELF_DIR
from loader import Skill, Character, parse_frontmatter

load_skill(folder: Path) -> Skill
load_skills(shelf_dir: Path) -> list[Skill]          # sub-folders only, sorted by name
load_character(path: Path) -> Character
load_characters(characters_dir: Path) -> list[Character]  # *.md except README.md, sorted
parse_frontmatter(text: str) -> (dict, str, list)    # (data, body, errors)
```

`Skill` fields: `name, description, version, version_source,
path, body, status ("valid"|"invalid"), errors`.
`Character` fields: `name, role, skills, tools, memory_scope,
prompt, path, status, errors`.
Both have `.ok` (bool) and `.error_codes()` (list of error codes).
Error entries are `{"code": ..., "message": ...}` dicts; codes
include `missing_skill_md, missing_frontmatter,
unclosed_frontmatter, malformed_frontmatter, malformed_list,
not_a_list, missing_name, name_mismatch, missing_description,
description_too_long, missing_version, missing_role,
missing_skills, missing_tools, missing_memory_scope,
invalid_memory_scope, empty_body, duplicate_key`.

## Commands

From `server/`:

```bash
python3 -m loader.cli skills                  # real shelf, defaults
python3 -m loader.cli characters              # runtime characters/ dir
python3 -m loader.cli check                   # both
python3 -m loader.cli skills --shelf DIR --json
python3 -m loader.cli characters --dir DIR --json
python3 -m unittest discover -s loader/tests -v   # tests
```

Exit code: 0 = everything listed is valid, 1 = at least one item
invalid (or a directory argument is missing). The real shelf is
expected to exit 1 because `crew-youtube-team` is a crew pack, not
a skill — that is the correct honest result, not a loader bug.
