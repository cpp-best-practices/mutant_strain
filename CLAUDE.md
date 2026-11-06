# Working on strain

Build and test: `pip install -e . && pytest`

## Changing the `--ui live` dashboard

Unit tests check strings in the frame. They can't tell you whether the layout reads well, so
look at the dashboard before you commit any change to `strain/dashboard.py`, `strain/console.py`
or `strain/keys.py`:

1. `pip install -e '.[ui-snapshot]'` (adds `pyte` and `pillow`).
2. `python tools/ui_snapshot.py tests/fixtures/sample_project --size 120x36 --keys down,tab,q --out ui-snapshots`
   - This needs the fixture's toolchain (CMake, a C++ compiler, gcov) and takes about a minute.
   - Also try a small size such as `--size 80x24` to check truncation and panel sizing.
3. Read the `ui-snapshots/*.txt` grids to check alignment, and view the `*.png` frames to check
   colours and reverse video. `pyte` drops SGR 2 (faint), so `DIM` text looks like normal text in the PNGs.
4. Check these points, which have regressed before:
   - The header totals match the survivors view, so each mutant is counted once across tests.
   - The progress bar and the title name the same test as the queue.
   - Each survivor detail row is padded to `DETAIL_LABEL_WIDTH`.
   - No panel leaves large blank areas while another panel is truncated.
5. Attach before and after frames, or the relevant `.txt` excerpt, to the pull request.
   Don't commit `ui-snapshots/`.

Keep iterations short: render the dashboard once, fix what you see, and render it once more to confirm.
