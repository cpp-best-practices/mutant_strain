# Working on strain

Build and test: `pip install -e . && pytest`

## Changing the `--ui live` dashboard

Unit tests check strings in the frame. They can't tell you whether the layout reads well, so
look at the dashboard before you commit any change to `strain/dashboard.py`, `strain/console.py`
or `strain/keys.py`:

1. `pip install -e '.[ui-snapshot]'` (adds `pyte` and `pillow`).
2. Configure a copy of the fixture as a git repository with a coverage build (strain refuses a folder
   without `CMakeCache.txt`), using the flags from `configure_project` in `tests/test_end_to_end.py`:
   ```sh
   demo=build/ui-demo && rm -rf $demo && mkdir -p $demo && cp -r tests/fixtures/sample_project $demo/project
   git -C $demo/project init -q && git -C $demo/project add . && git -C $demo/project -c user.name=strain -c user.email=strain@example.com commit -qm fixture
   cmake -S $demo/project -B $demo/build -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
     "-DCMAKE_CXX_FLAGS=--coverage -fsanitize=address,undefined -fno-omit-frame-pointer -D_GLIBCXX_ASSERTIONS -ftrivial-auto-var-init=pattern" \
     "-DCMAKE_EXE_LINKER_FLAGS=--coverage -fsanitize=address,undefined"
   cmake --build $demo/build
   python tools/ui_snapshot.py $demo/project --size 120x36 --keys down,tab,q --out ui-snapshots \
     -- --build-dir $PWD/$demo/build --report-dir $PWD/$demo/report
   ```
   - This needs the fixture's toolchain (CMake, a C++ compiler, gcov) and takes about a minute.
   - Arguments after `--` go to `strain run`.
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
