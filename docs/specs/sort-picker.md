## Problem Statement

The vault always shows session cards newest-first. That single order serves "what was I just doing?" but nothing else: there is no way to surface the oldest captures (the ones rotting at the back of the vault), and no way to see a session's neighbors — the other cards captured in the same directory — next to it. When a reader resumes work on a project, the cards from that project are interleaved with every other capture by raw timestamp, so finding "the rest of what I did there" means scanning or filtering by hand. The order is also invisible and unchangeable: nothing in the UI says how cards are sorted, and nothing lets the reader change it.

## Solution

A sort picker with three modes, reachable two ways — a `sort: <mode>` button in the toolbar (whose label always shows the active mode) and a `ctrl+s` keybinding — opening a dialog styled like opencode's theme picker: a list of options where only the active one carries a `●` marker, each with a dim one-line description, applied instantly on selection.

The three modes:

- **recent** — newest first (today's behavior, and the default).
- **oldest** — oldest first.
- **grouped** — cards bucketed by their capture directory (exact `cwd` match, the same keying Claude Code itself uses for its project storage); buckets ranked by their newest member, newest-first inside each bucket. The bucket containing the session you just captured leads, dragging its siblings up with it.

The live sort is session-only state. The *default* sort — what a fresh launch opens in — is a per-vault setting, editable from a new row in the existing ⚙ settings dialog and persisted in the vault's `settings.json` beside the scope default, so it travels with the vault wherever `$REWIND_DIR` points.

## User Stories

1. As a Rewind user, I want cards sorted newest-first by default, so that the app keeps behaving exactly as it does today unless I ask otherwise.
2. As a Rewind user, I want a `sort: recent` button in the toolbar, so that I can discover the sort feature without knowing any keybinding.
3. As a Rewind user, I want the sort button's label to always name the active mode, so that I can tell which order I'm looking at without opening anything.
4. As a Rewind user, I want `ctrl+s` to open the sort picker, so that I can change the order without leaving the keyboard.
5. As a Rewind user, I want the picker to list all three modes with a short description each, so that I understand what "grouped" means before choosing it.
6. As a Rewind user, I want only the currently-active mode to carry a `●` marker in the picker, so that the current state is unmistakable at a glance.
7. As a Rewind user, I want the picker's cursor to start on the active mode, so that the highlighted row and the marked row agree when the dialog opens.
8. As a Rewind user, I want to pick a mode with arrow keys and enter, so that one keystroke applies the sort and closes the dialog.
9. As a Rewind user, I want to pick a mode by clicking its row, so that the mouse path that opened the dialog can also finish the job.
10. As a Rewind user, I want `esc` to close the picker without changing anything, so that opening it just to check the options is free.
11. As a Rewind user, I want an `esc` hint at the bottom of the picker, so that the way out is advertised like in every other Rewind dialog.
12. As a Rewind user, I want "oldest" to show the oldest captures first, so that I can find and clean up what's been sitting in the vault longest.
13. As a Rewind user, I want "grouped" to pull every card from the same directory together, so that resuming one session shows me its siblings alongside it.
14. As a Rewind user, I want grouped buckets ordered by their newest member, so that the directory I most recently worked in leads the grid.
15. As a Rewind user, I want cards inside a bucket ordered newest-first, so that within a project the freshest session is on top.
16. As a Rewind user, I want changing the sort to keep my typed filter applied, so that I can narrow to a project and then reorder what's left.
17. As a Rewind user, I want changing the sort to respect the scope toggle, so that "only here" plus a sort order compose instead of fighting.
18. As a Rewind user, I want focus to return to the filter input after the picker closes, so that I can keep typing immediately, like after every other grid rebuild.
19. As a Rewind user, I want changing the sort to reorder what's already loaded rather than re-reading the vault, so that a sort change never doubles as a surprise sync — `ctrl+r` stays the only way to sync.
20. As a Rewind user, I want the sort button's label to update the moment I pick a mode, so that the toolbar never lies about the active order.
21. As a Rewind user, I want the picker to open even on an empty vault, so that no key silently does nothing.
22. As a Rewind user, I want broken cards to stay visible in every mode, so that "never silently dropped" holds regardless of sort.
23. As a Rewind user, I want a sort-default row in the ⚙ settings dialog, so that I can make "grouped" (or any mode) what the app opens in.
24. As a Rewind user, I want the ⚙ sort row to look like the live picker, so that the two dialogs read as one feature.
25. As a Rewind user, I want picking a mode in ⚙ to only move the pending marker, so that nothing changes until I press Save.
26. As a Rewind user, I want Cancel in ⚙ to discard my pending sort choice, so that backing out is always safe.
27. As a Rewind user, I want the saved default to apply on the next launch, so that my preference outlives the session.
28. As a Rewind user, I want the default stored in the vault's own `settings.json`, so that it travels with the vault when `$REWIND_DIR` points elsewhere.
29. As a Rewind user, I want saving the sort default to preserve my scope default (and vice versa), so that one setting never silently erases another.
30. As a Rewind user, I want a corrupt or hand-edited `settings.json` to silently fall back to "recent", so that no state of that file can block the vault from opening.
31. As a Rewind user, I want changing the live sort to leave the stored default untouched, so that a temporary reorder is never accidentally permanent.
32. As a Rewind user, I want the README to document the sort feature, the keybinding, and the settings key, so that the feature is discoverable outside the app.

## Implementation Decisions

- **Three modes, no more.** `recent` (captured-at descending — current behavior and the fallback everywhere), `oldest` (ascending), `grouped`. Title/repo/model/harness sorts were considered and rejected: repo is a capture-time basename that wrongly merges unrelated same-named projects, and the rest are grouping keys better served by the filter.
- **Grouped keys on exact `cwd` match**, consistent with `same_dir` (already exact-match by documented decision) and with Claude Code's own project storage, which slugifies the full cwd. Not repo-based, not prefix-based: a wrong merge (two unrelated projects fused) is worse than a wrong split (one project's subdirectories in adjacent buckets, which recency ranking parks next to each other anyway).
- **Broken cards get no special-casing.** They sort by their existing fallbacks (epoch timestamp, empty cwd): bottom in recent (unchanged from today), top in oldest, one nameless trailing bucket in grouped. Pinning was considered and declined to keep this change minimal.
- **Sorting is extracted into a pure function** over a list of sessions, parameterized by mode. `load_vault` calls it with the default; the sort-change path calls it over the in-memory list and never touches the disk. Reordering re-mounts the card grid (order is mount order), then re-applies the current filter query and refocuses the filter input — identical post-conditions to the existing reload path, ideally by sharing its rebuild code.
- **Live sort is session-only state; the default is persisted.** No `$REWIND_SORT` env var — a dialog cannot set an env var, and two mechanisms for one preference is a review flag. The default lives as a new key in the vault's `settings.json` beside the scope default.
- **Settings persistence generalizes to a whole-dict load/save pair** (merge-on-write), replacing the single-key scope functions — the change their own documentation demands the moment a second key appears. Failure handling follows the established rule: any unreadable, malformed, or unknown value silently yields the default; nothing in settings may block launch. (This deliberately differs from the loud-toast handling designed for the abandoned env-var approach.)
- **Both pickers are OptionList-based and visually unified**, opencode-theme-picker style: single-line rows, dim `—`-separated descriptions, `●` prefix on the active row only, no markers elsewhere, no search box (three options don't need one), esc hint at the bottom per house convention. RadioSet was considered for the ⚙ row and rejected in favor of visual unification.
- **Same look, different commit semantics.** The live picker applies-and-dismisses on selection. The ⚙ row only moves the pending marker (prompt replacement on selection); the write happens on Save, and Cancel discards — matching the settings dialog's existing stage-then-commit contract.
- **Toolbar order: filter, sort, scope, gear.** The sort button spells out its state (`sort: recent` / `sort: oldest` / `sort: grouped`), matching the scope button's label-is-the-state convention; an icon-only glyph was rejected because it cannot show state. Clicking it opens the same picker as `ctrl+s`.
- **`ctrl+s` is an app-level priority binding**, same tier as `ctrl+r`/`ctrl+f` (bare letters belong to the filter input), safe because the terminal driver disables XON/XOFF flow control. It opens unconditionally, including on an empty vault — a conditional no-op key reads as broken.

## Testing Decisions

- **Test external behavior only**: the order cards appear in the grid, the button label, what `settings.json` contains after Save, what order a fresh launch opens in — never the internals of how the list was sorted or which widget produced the click.
- **Primary seam: the Textual pilot** (`run_test`), the seam the entire app test suite already uses. Cover: `ctrl+s` and button-click both open the picker; selecting a row reorders the grid, updates the button label, dismisses, keeps the typed filter applied, and returns focus to the filter; esc changes nothing; picking a sort does not pick up files added to the vault on disk (no implicit sync); the ⚙ flow writes the default on Save and not on Cancel; an app launched over a vault with a seeded `settings.json` opens in the stored order; corrupt settings still launch in recent. Prior art: the existing reload tests (filter survival, focus return, empty-vault) and settings-dialog tests.
- **Secondary seam: direct module-function tests**, as the vault test module already does for matching and path helpers. Cover the pure sort function per mode with fabricated sessions — including broken sessions' fallback placement and grouped's bucket-by-newest-member ranking — and the generalized settings load/save round-trip (merge preserves the other key; every corruption shape yields defaults) with a temp directory, mirroring the existing scope-default tests.
- No new seam types are introduced.

## Out of Scope

- A Footer or any general keybinding-discoverability work.
- `$REWIND_SORT` or any environment-variable control of sorting (explicitly abandoned in favor of `settings.json`).
- Repo-based or prefix/subdirectory-aware grouping.
- Pinning broken cards to a fixed position across modes.
- Persisting the *live* sort across quits (only the default persists; the session's choice dies with the session).
- Additional sort keys (title, repo, model, harness) or reverse toggles per mode.
- A search/filter box inside the picker.
- Watching the vault directory or any change to when the vault is read.

## Further Notes

- The picker's visual reference is opencode's theme picker (marker on the active item only, full-width cursor bar on the highlighted row); the two deliberate departures — no search box, esc hint at the bottom rather than top-right — keep it consistent with Rewind's own dialog conventions.
- The grouped mode's mental model, in one line: still recency-ordered, but a session drags its directory-siblings up alongside it instead of interleaving strictly by timestamp.
- After this change the settings file carries two keys; any future third key inherits the merge-on-write behavior for free.
