# gh-sticky

<!-- A table rather than <img align="right">: the float is ignored by some
     previews (VS Code); a table lays out the same everywhere. -->
<table>
<tr>
<td valign="top">

A small always-on-top window that keeps hand-picked GitHub pull requests and
issues in front of you, grouped in panels, styled after the Changes list in the
Claude desktop app.

- Panels are created, renamed and reordered inline (hover a panel title for its
  actions, double-click it to rename).
- Items are added by hand: paste one or more PR/issue links (or
  `owner/repo#123`) into a panel's add box. No filters or searches.
- Each item shows its title, `repo #number`, `+additions −deletions` for open
  PRs, and an icon for its state: CI result for open PRs (passed, failed,
  running), draft, merged, closed, or the issue state.
- Click an item to open it in the browser; right-click or `⋯` for copy, move
  and remove.
- Refreshes every 5 minutes (or with the refresh button) through one
  `gh api graphql` call.

</td>
<td valign="top" width="300">
<img src="docs/screenshot.png" alt="gh-sticky window with two panels of PRs and issues" width="300">
</td>
</tr>
</table>

## Requirements

- Python 3.9+ with tkinter (included on Windows and in the python.org macOS
  installer; on Linux install `python3-tk` or your distribution's equivalent).
- [GitHub CLI](https://cli.github.com/) installed and logged in
  (`gh auth login`). The tool stores no tokens; it only calls `gh`.

No other packages.

## Run

```bash
# Windows, without a console window
pythonw gh-sticky.pyw
# Linux / macOS
python3 gh-sticky.pyw
# Use another state file, e.g. one per project or a synced folder
python3 gh-sticky.pyw --state ~/Dropbox/gh-sticky.json
```

## State

Panels, items, the last fetched data and the window geometry are saved to the
file given with `--state`, or by default to:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\gh-sticky\state.json` |
| macOS | `~/Library/Application Support/gh-sticky/state.json` |
| Linux | `$XDG_CONFIG_HOME/gh-sticky/state.json` (default `~/.config/...`) |

## Platform notes

Developed and tested on Windows 11. Linux and macOS use fallbacks (default
font, plain-text button glyphs, X11 wheel buttons, Button-2 right click on
macOS) but have not been tried yet. On Wayland, the compositor may ignore
always-on-top.
