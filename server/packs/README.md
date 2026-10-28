# packs/ — Plugin-pack format (Dots-style)

One pack = one folder on the skill shelf (`~/workspace/grokbot/skill-pack/<name>/`).

## Files in a pack

| File | Meaning |
|---|---|
| `SKILL.md` | kind=`skill`: frontmatter + body (the loader's format). |
| `characters/` + `README.md` | kind=`crew`: one `.md` per character + crew description. |
| `template.json` + `README.md` | kind=`template`: a staged crew definition (see `templates/README.md`). |
| `SOURCE.md` | Required when imported from outside: origin repo + license. Absent = own build. |
| `manifest.json` | Written by the converter; read by the validator. **The loader ignores it.** |

## manifest.json

```json
{
  "name": "threads-substance",
  "kind": "skill",
  "version": "1.0.0",
  "description": "…(≤1024 chars)…",
  "skills": ["threads-substance"],
  "characters": [],
  "source": {"origin": "https://github.com/…", "license": "MIT"},
  "mcp": {"servers": {}},
  "review": {"required_before": ["send", "publish", "merge", "deploy", "delete", "change-production-data"]},
  "requires": []
}
```

## Rules (enforced by `pack.py`)

1. **No secrets in manifests.** Any key whose name looks like a credential
   (`secret|token|password|api_key|client_secret|…`) anywhere in the document
   fails validation. Credentials stay BYO (browser localStorage only).
2. **Human review is mandatory.** `review.required_before` must contain all
   six default actions (send, publish, merge, deploy, delete,
   change-production-data). A pack may add more, never fewer.
3. `name` must equal the folder name; `description` ≤ 1024 chars.
4. kind=`skill` → SKILL.md must exist and pass the loader; `skills` must be
   exactly `[name]`; `characters` must be empty.
5. kind=`crew` → `characters/` must hold ≥1 `.md` file (every listed stem must
   exist) and README.md must exist; `skills` is the crew's declared
   shelf-skill dependencies (empty in Phase 1 — crew character files are
   plain markdown with no skill declarations, so the manifest invents none).
6. kind=`template` → `template.json` must exist and mirror the manifest
   (same name/version/description, same character names; every character
   needs a non-empty name, role, and prompt) and README.md must exist.
   The no-secrets rule also covers `template.json` itself. Import only
   *stages* the pack — its characters are not live until installed
   explicitly (see `templates/README.md`). The `grokbot.team`
   package rules apply here too, enforced by the validator the
   same way the importer enforces them: a `format` marker other
   than `"grokbot.team"` is rejected; forbidden fields (chat
   history, secrets, model choice, computer, visibility) anywhere
   in `template.json` or the manifest are rejected; secret-like
   values in `template.json` are rejected (never echoed); at most
   60 distinct skills per package; `routines` (character must be
   one of the template's own, schedule must validate) and
   `connections` (http/stdio, url/command) are validated
   structurally; and every connection the manifest declares in
   `mcp.servers` must carry `"enabled": false` — template
   connections start disabled until a human approves them.

## Commands (from `server/`)

```bash
python3 -m packs.convert --shelf ../grokbot/skill-pack            # dry run
python3 -m packs.convert --shelf ../grokbot/skill-pack --write     # write manifest.json
python3 -m packs.convert --shelf ../grokbot/skill-pack --check     # validate existing manifests
python3 -m unittest discover -s packs/tests -v
```

The converter is additive (only writes `manifest.json`) and idempotent
(two runs produce byte-identical files). The `.zip` snapshots on the shelf
are not touched.

Full spec: `NEXT-LEVEL-PLAN.md` §4.
