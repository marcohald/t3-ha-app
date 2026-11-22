# Bundled skills

- `homeassistant/` is a vendored copy of
<https://github.com/marcohald/ha-agent-skill> (upstream commit
`a1711ee948dfd6e9e1807ac3a05d6b0bdaba31f1`), pinned so an add-on build never
depends on GitHub being reachable and never picks up an unreviewed upstream
change.

Only `SKILL.md`, `README.md`, `scripts/` and `references/` are vendored.
Upstream's `tests/`, `eval/` and `CHANGELOG.md` are development material and
stay out of the image.

## Refresh from upstream

```bash
./skill/update-skill.sh                # newest master
./skill/update-skill.sh --rev <sha>    # pin a specific commit
```

The script re-copies the tracked paths, prints the diff, and reminds you to bump
the add-on version and changelog. Review the diff before committing: the skill
runs with access to the Home Assistant API.

## Install at runtime

`linux-packages/` is maintained in this repository: it lets an agent install
missing apt packages and, after asking the user, persist them in the add-on's
extra-package list so they are reinstalled after every update.

`skills.sh` (copied to `/opt/t3code/skills.sh`) installs **all** bundled skills
into the persistent `/data` volume on every container start, so they survive
add-on updates:

| Provider    | Directory                                          |
| ----------- | -------------------------------------------------- |
| Claude      | `~/.claude/skills/homeassistant`                   |
| Codex       | `~/.codex/skills/homeassistant`                    |
| OpenCode    | `~/.config/opencode/skills/homeassistant`          |
| fx          | `~/.agents/skills/homeassistant`, `~/.claw/skills` |
| Antigravity | `~/.gemini/config/skills/homeassistant`            |

`$HOME` is `/data`, so every path above is on the persistent volume. Agents
are also told about the skill in their memory files (`CLAUDE.md` / `AGENTS.md`,
written by `persist.sh`), so they reach for it without being asked.

The **Update skill** button in Quick Access clones the newest upstream copy
over the installed one (`GET /t3_skill?action=update`).
