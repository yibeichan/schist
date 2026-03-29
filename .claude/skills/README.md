# RollUp Skills

Structured workflow skills adapted from [gstack](https://github.com/yibeichan/gstack) (Garry Tan's Claude Code workflow toolkit).

## Upstream

Upstream source: `~/Projects/GitHub/gstack`

To update a skill (copies full dir, removes dev-only .tmpl files):
```bash
cd ~/Projects/GitHub/gstack && git pull
for skill in review investigate cso retro careful freeze guard plan-eng-review; do
  rm -rf ~/Projects/GitHub/rollup/.claude/skills/$skill
  cp -r ~/Projects/GitHub/gstack/$skill ~/Projects/GitHub/rollup/.claude/skills/$skill
  rm -f ~/Projects/GitHub/rollup/.claude/skills/$skill/SKILL.md.tmpl
done
```

## Available Skills

| Skill | Who uses it | When |
|-------|-------------|------|
| `/review` | 🦞 Sansan | Self-check before sending diff to Ninjia |
| `/investigate` | 🦞 Sansan | Before any bug fix — root cause first, Iron Law |
| `/cso` | 🥷 Ninjia | On security-sensitive PRs (auth, RLS, secrets, uploads) |
| `/retro` | 11️⃣ Eleven | Weekly — test health trends, shipping streaks |
| `/careful` | All | Before destructive ops (migrations, force-push, DROP TABLE) |
| `/freeze` | All | Lock edits to one directory during sensitive work |
| `/guard` | All | `/careful` + `/freeze` together — use during release windows |
| `/plan-eng-review` | 🦞 Sansan | Before implementing any task >50 LOC — lock architecture first |

## Philosophy

See `ETHOS.md` for the full builder philosophy:
- **Boil the Lake** — do the complete thing, not the 90% shortcut
- **Search Before Building** — 3-layer knowledge check before designing anything
