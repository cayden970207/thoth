# Releasing Thoth

How a new version of Thoth reaches people, and the rules that keep each release easy to follow.

## Version numbers

- `plugin.yaml` holds the version. Everything else reads it: `thoth_core.__version__`, the Thoth page and the
  catalog entry.
- A fix bumps the last number (0.9.0 → 0.9.1). Something people will notice bumps the middle one
  (0.9.1 → 0.10.0). 1.0 comes once Thoth is stable.
- `main` only gets releases. Someone who installed Thoth straight from GitHub gets every push to `main` as an
  update, so work happens on branches and lands on `main` as a finished version.

## What's new

Every version has a section in [CHANGELOG.md](../CHANGELOG.md): a `## X.Y.Z — YYYY-MM-DD` heading, then `- `
lines written for the person using Thoth, not for developers. After an update the Thoth page shows the first
three lines once, so lead with what people will notice. The tests fail when the newest section isn't the
version in `plugin.yaml`.

## Steps

1. Bump `version:` in `plugin.yaml` and write the version's section in `CHANGELOG.md`.
2. Check it:

   ```bash
   uvx --python 3.12 --with fastapi pytest -q tests
   hermes plugins validate .
   ```

3. Commit as `X.Y.Z: <what it brings>` and push `main`.
4. Tag the commit and publish a GitHub release with the same words as the changelog:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "Thoth X.Y.Z" --notes "$(python3 -m thoth_core.changelog X.Y.Z)"
   ```

5. Move the catalog pin to the release commit:

   ```bash
   python3 scripts/catalog_entry.py "$(git rev-parse HEAD)" thoth.yaml
   ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/hermes-agent/scripts/validate_plugin_catalog.py thoth.yaml
   ```

   The file goes in `plugin-catalog/thoth.yaml` of NousResearch/hermes-agent: update the open catalog PR
   (NousResearch/hermes-agent#150497), or open a pin-bump PR once it is merged. People who installed Thoth from
   the catalog get the update after that PR is merged.

## How people get it

- Hermes checks installed plugins once a day and shows **Plugin updates available**, with **Update** in
  Settings → Plugins (or `hermes plugins update thoth`). With `hermes plugins auto-update on`, it installs
  updates by itself.
- Once the new version loads, the Thoth page shows what's new, once. `thoth_core/changelog.py` remembers the
  last version each person has seen in `whatsnew.json`, in Thoth's data folder. A new install starts at its
  own version, so nobody's first project opens with a list of changes.
