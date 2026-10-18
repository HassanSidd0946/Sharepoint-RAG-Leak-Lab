"""The summary as JSON and as one static HTML page (Jinja2, autoescaped, no scripts).

report.json is the summary model dumped as it is (every number the HTML shows is in it). The HTML
template only formats: it holds no figures of its own. Journal names in the page are relative
links, so the page works from the benchmark folder.
"""

import json
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from leaklab.benchmark.aggregate import BenchmarkSummary, Cell

JSON_NAME = "report.json"
HTML_NAME = "report.html"


def render_html(summary: BenchmarkSummary) -> str:
    env = Environment(
        loader=PackageLoader("leaklab.benchmark", "templates"),
        autoescape=select_autoescape(["html", "j2"], default=True),
        undefined=StrictUndefined,  # a typo in the template is an error, not an empty cell
        trim_blocks=True,
        lstrip_blocks=True,
    )
    scenarios: list[str] = []
    by_scenario: dict[str, list[Cell]] = {}
    for cell in summary.cells:
        if cell.scenario not in by_scenario:
            scenarios.append(cell.scenario)
        by_scenario.setdefault(cell.scenario, []).append(cell)
    labels = sorted(set(summary.system_labels.values()))
    return env.get_template("report.html.j2").render(
        s=summary, scenarios=scenarios, cells_by_scenario=by_scenario, labels=labels
    )


def write_report(summary: BenchmarkSummary, folder: Path) -> tuple[Path, Path]:
    json_path, html_path = folder / JSON_NAME, folder / HTML_NAME
    json_path.write_text(json.dumps(summary.model_dump(), indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(summary), encoding="utf-8")
    return json_path, html_path
