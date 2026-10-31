# mutant_strain

**Mutation testing for C++ projects built with CMake and tested with CTest.**

strain changes your code in small ways (`<` becomes `<=`, `&&` becomes `||`) and checks whether your tests notice. Every mutant that survives points at a test that runs the code but doesn't really check it.

> **Status:** design stage, not yet usable.
> Design doc: <!-- TODO(claude): link the design doc -->

<!--
Notes for whoever fills this in (human or Claude Code):
- Every `TODO(claude)` marks a gap to fill once the code exists.
- Keep the README short and bullet-driven; deep detail belongs in docs/.
- When a section is filled, remove its TODO marker.
-->

---

## Why strain

- **Results in minutes:** tests are mutated one at a time, so you see the first results without a full-suite baseline.
- **Works with your toolchain:** GCC or Clang, Ninja or Makefiles. No compiler plugin required.
- **Sanitizers count:** a mutant caught by ASan or UBSan counts as killed, and is reported separately.
- **Action items, not just a score:** pseudo-tested functions, tests that kill nothing, and a concrete hint for each survivor.
- **Minimal dependencies:** the core is the Python standard library only.

## Requirements

- Python 3.11+ <!-- TODO(claude): confirm minimum version -->
- CMake 3.14+ and CTest
- git, with a clean working tree
- GCC 9+ with `gcov` (v1), or Clang with `llvm-cov` (planned)
- Optional:
  - `cppcheck`: the best mutation engine
  - `tree-sitter`: the fallback engine (pip)
  - `rich`: the live dashboard (pip)

## Install

```sh
# TODO(claude): replace once packaging exists
pip install strain            # core
pip install "strain[ui]"      # with the live dashboard
```

## Quick start

### 1. Configure a build for strain

strain needs coverage, sanitizers and a compilation database:

```sh
cmake -S . -B build-strain -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_CXX_FLAGS="--coverage -fsanitize=address,undefined -fno-omit-frame-pointer -D_GLIBCXX_ASSERTIONS -ftrivial-auto-var-init=pattern" \
  -DCMAKE_EXE_LINKER_FLAGS="--coverage -fsanitize=address,undefined"
cmake --build build-strain
```

<!-- TODO(claude): add a CMakePresets.json example and a Clang variant -->

### 2. Run it

```sh
strain run --build-dir build-strain              # all tests
strain run --build-dir build-strain -R parser    # only tests matching "parser"
strain run --build-dir build-strain --since main # only lines changed since main
```

### 3. Read the results

<!-- TODO(claude): paste a real console summary once one exists -->

```text
parser_tests   score 87.8%   killed 31  sanitizer 4  timeout 1  survived 5

Action items
  1. parse_header() is pseudo-tested: nothing checks its result
  2. lexer.cpp:88  `==` → `!=` survived (not_reached): add a test where tok != END
  3. parser.cpp:97 `+` → `-` survived (not_detected): assert the computed offset
```

## How it works

For each selected test:

1. **Baseline:** run the test unmutated and collect its coverage. If it fails, reports UB or times out, it's excluded.
2. **Generate mutants** on the lines this test executed, in a fixed, reproducible order.
3. For each mutant:
   1. Mutate the source, with the original saved to a journal first.
   2. Reject it early if `-fsyntax-only` fails.
   3. Rebuild incrementally with `cmake --build`.
   4. Run the test and classify the result.
   5. Restore the file, always, even on Ctrl-C.

Interrupted runs continue with `--resume`.

## Result states

| State | Meaning | Counts toward score |
| --- | --- | --- |
| `killed` | A test failed | Yes |
| `killed_by_sanitizer` | ASan or UBSan caught it, the assertions didn't | Yes, flagged |
| `timeout` | Ran too long | Yes |
| `survived` | All tests passed | Yes |
| `skipped_unevaluated` | Operand never evaluated by this test | No |
| `compile_error` | Didn't compile | No |
| `ignored` | Suppressed by config or `// mutate-off` | No |

Survivors are labeled `not_reached` (needs a new test input) or `not_detected` (needs a stronger assertion) when branch coverage is available.

Mutation score = (killed + killed_by_sanitizer + timeout) ÷ (those + survived)

## Safety nets

strain checks every translation unit for:

- ASan
- UBSan
- standard library assertions
- pattern auto-init

Each check is `error`, `warn` or `ignore`; the default is `error`.

```sh
strain run --safety-nets=warn
```

<!-- TODO(claude): document per-check overrides in the config file -->

## CLI reference

<!-- TODO(claude): generate from the real argument parser -->

| Option | Default | Description |
| --- | --- | --- |
| `--build-dir` | `.` | CMake build folder |
| `-R` / `-E` | | Include / exclude tests by regex, as in ctest |
| `--since <ref>` | | Mutate only lines changed since a git ref |
| `--strategy` | `per-test` | `per-test` or `per-mutant` |
| `--skip-killed` | off | Skip mutants an earlier test already killed |
| `--engine` | `auto` | `cppcheck`, `tree-sitter`, `lexer` |
| `--coverage` | `auto` | `gcov`, `llvm-cov`, `lcov`, `gcovr` |
| `--ui` | `auto` | `live`, `log`, `json` |
| `--safety-nets` | `error` | `error`, `warn`, `ignore` |
| `--threshold` | | Exit non-zero below this score |
| `--strict` | off | Abort if any test is excluded |
| `--max-per-line` | | Cap mutants per source line |
| `--resume` | | Continue an interrupted run |
| `--all` | off | Show every survivor, not just the top actions |

## Configuration

<!-- TODO(claude): finalize file name, schema and precedence vs CLI -->

```toml
# strain.toml
exclude = ["tests/**", "third_party/**"]
arid = ["LOG_*", "spdlog::*"]

[safety_nets]
asan = "error"
ubsan = "error"
stdlib_assertions = "error"
auto_var_init = "warn"

[ignore]
mutants = ["a1b2c3d4"]   # equivalent mutants, by ID
```

To suppress mutants in source, add `// mutate-off` to the line.

## Reports

Written to `strain-report/` <!-- TODO(claude): confirm path -->:

- `report.json`: full results, in the mutation-testing-elements schema
- `events.jsonl`: every event of the run
- compiler-style warnings on the console (`file:line:col: warning: survived [ror]`), clickable in editors

## Pluggable backends

| Part | Options |
| --- | --- |
| Mutation engine | `cppcheck`, `tree-sitter`, `lexer` |
| Coverage | `gcov`, `llvm-cov`, `lcov`, `gcovr` |
| Strategy | `per-test`, `per-mutant` |
| UI | `live`, `log`, `json` |

<!-- TODO(claude): link docs/backends.md with the interfaces once written -->

## Limitations

- Tests must be deterministic
- One build per mutant, so large header changes are slow
- MSVC is not supported
- No parallel workers yet

## Roadmap

- [ ] v1: GCC + gcov, cppcheck/lexer engines, per-test strategy, log/json/live UI
- [ ] Clang + llvm-cov, tree-sitter engine
- [ ] Condition/region coverage to skip unevaluated operands
- [ ] per-mutant strategy and parallel workers
- [ ] Equivalent-mutant detection by comparing object files
- [ ] Mutant schemata (no rebuild per mutant)

## Contributing

<!-- TODO(claude): dev setup, running strain's own tests, code layout -->

## License

<!-- TODO(claude): choose a license -->
