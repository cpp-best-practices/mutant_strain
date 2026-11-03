"""`index.html`: one self-contained page with the score, a table of files and the source annotated with its mutants."""

from collections.abc import Iterable, Mapping, Sequence
from html import escape

from .console import HIGH_SCORE, LOW_SCORE
from .report import TAB_WIDTH, FileScore, file_scores, format_score, hint
from .results import DETECTED_STATES, CombinedResult, MutantState, mutation_score

KILLED_CLASS = "killed"
SURVIVED_CLASS = "survived"
UNSCORED_CLASS = "unscored"
STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 75rem; color: #1f2328; padding: 0 1rem; }
h1 { margin-bottom: 0.25rem; }
.score { font-size: 2.5rem; font-weight: bold; }
.high { color: #1a7f37; } .medium { color: #9a6700; } .low { color: #cf222e; } .none { color: #656d76; }
table { border-collapse: collapse; }
.files td, .files th { padding: 0.25rem 0.75rem; text-align: right; border-bottom: 1px solid #d0d7de; }
.files td:first-child, .files th:first-child { text-align: left; }
.source { width: 100%; font-family: ui-monospace, monospace; font-size: 0.85rem; }
.source pre { margin: 0; white-space: pre-wrap; }
.number { color: #656d76; text-align: right; padding-right: 0.75rem; user-select: none; vertical-align: top; }
.number a { color: inherit; text-decoration: none; }
.line.killed { background: #dafbe1; } .line.survived { background: #ffebe9; } .line.unscored { background: #f6f8fa; }
.mutants ul { margin: 0.25rem 0 0.5rem; padding-left: 1.25rem; font-family: system-ui, sans-serif; }
.state { font-weight: bold; } .killed .state { color: #1a7f37; } .survived .state { color: #cf222e; }
.unscored .state { color: #656d76; }
.detail { color: #656d76; }
:target { outline: 2px solid #0969da; }
"""


def _score_class(score: float | None) -> str:
    if score is None:
        return "none"
    if score * 100 >= HIGH_SCORE:
        return "high"
    return "medium" if score * 100 >= LOW_SCORE else "low"


def _state_class(state: MutantState) -> str:
    if state is MutantState.SURVIVED:
        return SURVIVED_CLASS
    return KILLED_CLASS if state in DETECTED_STATES else UNSCORED_CLASS


def _line_class(entries: Sequence[CombinedResult]) -> str:
    classes = {_state_class(entry.state) for entry in entries}
    return next(name for name in (SURVIVED_CLASS, KILLED_CLASS, UNSCORED_CLASS) if name in classes)


def _anchor(file: str, line: int) -> str:
    return escape(f"{file}:{line}")


def _change(entry: CombinedResult) -> str:
    mutant = entry.mutant
    return f"<code>{escape(mutant.original)}</code> → <code>{escape(mutant.replacement)}</code>"


def _explanation(entry: CombinedResult) -> str:
    if entry.state in DETECTED_STATES:
        return f"killed by {escape(', '.join(entry.killed_by))}"
    if entry.state is MutantState.SURVIVED:
        return f"{escape(hint(entry))}; covered by {escape(', '.join(entry.covered_by))}"
    return ""


def _mutant_item(entry: CombinedResult) -> str:
    mutant = entry.mutant
    survivor = "" if entry.survivor is None else f" ({escape(entry.survivor)})"
    return (
        f'<li class="{_state_class(entry.state)}"><span class="state">{escape(entry.state)}</span>{survivor} '
        f"{_change(entry)} <span class=\"detail\">column {mutant.column} [{escape(mutant.operator)}] id {mutant.id}</span>"
        f" {_explanation(entry)}</li>"
    )


def _source_rows(file: str, source: str, entries: Iterable[CombinedResult]) -> list[str]:
    by_line: dict[int, list[CombinedResult]] = {}
    for entry in entries:
        by_line.setdefault(entry.mutant.line, []).append(entry)
    rows: list[str] = []
    for number, text in enumerate(source.splitlines(), start=1):
        mutants = by_line.get(number, [])
        line_class = f"line {_line_class(mutants)}" if mutants else "line"
        rows.append(
            f'<tr id="{_anchor(file, number)}" class="{line_class}"><td class="number">'
            f'<a href="#{_anchor(file, number)}">{number}</a></td>'
            f"<td><pre>{escape(text.expandtabs(TAB_WIDTH))}</pre></td></tr>"
        )
        if mutants:
            items = "".join(_mutant_item(entry) for entry in mutants)
            rows.append(f'<tr class="mutants"><td></td><td><ul>{items}</ul></td></tr>')
    return rows


def _file_row(row: FileScore) -> str:
    return (
        f'<tr><td><a href="#{escape(row.file)}">{escape(row.file)}</a></td><td>{row.mutants}</td>'
        f"<td>{row.detected}</td><td>{row.survived}</td>"
        f'<td class="{_score_class(row.score)}">{format_score(row.score)}</td></tr>'
    )


def _files_table(combined: Sequence[CombinedResult]) -> list[str]:
    return [
        "<h2>Files</h2>",
        '<table class="files"><tr><th>File</th><th>Mutants</th><th>Detected</th><th>Survived</th><th>Score</th></tr>',
        *(_file_row(row) for row in file_scores(combined)),
        "</table>",
    ]


def _survivor_list(combined: Sequence[CombinedResult]) -> list[str]:
    survivors = [entry for entry in combined if entry.state is MutantState.SURVIVED]
    if not survivors:
        return []
    links = (
        f'<li><a href="#{_anchor(entry.mutant.file, entry.mutant.line)}">'
        f"{escape(entry.mutant.file)}:{entry.mutant.line}:{entry.mutant.column}</a> {_change(entry)}: "
        f"{escape(hint(entry))}</li>"
        for entry in survivors
    )
    return [f"<h2>Survivors ({len(survivors)})</h2>", "<ul>", *links, "</ul>"]


def _file_sections(combined: Sequence[CombinedResult], sources: Mapping[str, str]) -> list[str]:
    sections: list[str] = []
    for row in file_scores(combined):
        entries = [entry for entry in combined if entry.mutant.file == row.file]
        sections += [
            f'<details open id="{escape(row.file)}"><summary><h2 style="display: inline">{escape(row.file)}</h2> '
            f"{format_score(row.score)}</summary>",
            '<table class="source">',
            *_source_rows(row.file, sources[row.file], entries),
            "</table></details>",
        ]
    return sections


def _counts(combined: Sequence[CombinedResult]) -> str:
    states = [entry.state for entry in combined]
    return ", ".join(f"{escape(state)} {states.count(state)}" for state in MutantState if state in states)


def html_report(combined: Sequence[CombinedResult], sources: Mapping[str, str]) -> str:
    score = mutation_score(entry.state for entry in combined)
    body = (
        [f"<p>{len(combined)} mutants: {_counts(combined)}</p>", *_files_table(combined), *_survivor_list(combined),
         *_file_sections(combined, sources)]
        if combined
        else ["<p>No mutants were generated.</p>"]
    )
    page = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>strain mutation report: {format_score(score)}</title>",
        f"<style>{STYLE}</style></head><body>",
        "<h1>strain mutation report</h1>",
        f'<div class="score {_score_class(score)}">{format_score(score)}</div>',
        *body,
        "</body></html>",
    ]
    return "\n".join(page) + "\n"
