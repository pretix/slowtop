#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import locale
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional, Tuple

import pglast
import psycopg
import requests
import rich.text
from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical
from textual.screen import Screen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header,
                             Input, Label, RadioButton, RadioSet, Select,
                             TextArea)
from textual.widgets.data_table import ColumnKey

try:
    from config import API_URL, EXTRA_FIELDS, LOCALE, LOG_DIR, PG_CONNINFO
except ImportError:
    print("Error: config.py not found. Please create it based on config.py.example.")
    sys.exit(1)
try:
    from config import CACHE_SIZE
except ImportError:
    # Usually results in <100 MB ram, but still a useful amount of caching in many cases
    CACHE_SIZE = 1024

locale.setlocale(locale.LC_ALL, LOCALE)

APP_NAME = "slowtop"
VERSION = "0.1.0"


class Text(rich.text.Text):
    pass


class Number(rich.text.Text):
    def __init__(
        self,
        value: float | int | None,
        justify: Literal["default", "left", "center", "right", "full"] | None = "right",
        *args,
        **kwargs,
    ) -> None:
        self.value = value
        if value is None:
            value_str = "-"
        else:
            value_str = f"{round(value):n}"
        super().__init__(value_str, justify=justify, *args, **kwargs)


@dataclass(slots=True, unsafe_hash=True)
class SlowQuery:
    time: str
    runtime_ms: float
    ps: str
    sql: str
    plan: str
    query_id: str
    extra_fields: dict[str, str] = field(default_factory=dict, hash=False)


@dataclass(slots=True, unsafe_hash=True)
class QueryGroup:
    query_id: str
    sql: str
    plan: str
    count: int
    total_runtime_ms: float
    avg_runtime_ms: float
    max_runtime_ms: float
    extra_fields: dict[str, str] = field(default_factory=dict, hash=False)


COLUMNS_PLAIN = [
    {"name": "Time", "key": "time", "justify": "left"},
    {"name": "Runtime [ms]", "key": "runtime", "justify": "right"},
    {"name": "PS", "key": "ps", "justify": "left"},
    {"name": "SQL", "key": "sql", "justify": "left"},
    {"name": "QueryId", "key": "query_id", "justify": "left"},
]
COLUMNS_PLAIN += EXTRA_FIELDS
COLUMNS_GROUPED = [
    {"name": "Count", "key": "count", "justify": "right"},
    {"name": "Avg [ms]", "key": "avg_runtime", "justify": "right"},
    {"name": "Max [ms]", "key": "max_runtime", "justify": "right"},
    {"name": "Total [ms]", "key": "total_runtime", "justify": "right"},
    {"name": "QueryId", "key": "query_id", "justify": "left", "aggregation": "count"},
    {"name": "SQL", "key": "sql", "justify": "left", "aggregation": "sample"},
] + EXTRA_FIELDS


def osc52_copy(text: str, driver=sys.stdout) -> None:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    driver.write(f"\033]52;c;{encoded}\a")
    driver.flush()


def filter_text(text: str, filter_str: str, filter_mode: str, inverse: bool):
    if filter_mode == "exact":
        if filter_str != text:
            return not inverse
    elif filter_mode == "substring":
        if filter_str not in text:
            return not inverse
    elif filter_mode == "regex":
        if not re.search(filter_str, text):
            return not inverse
    else:
        raise ValueError(f"Invalid filter_mode: {filter_mode}")
    return inverse


def run_explain_analyze(sql: str) -> str:
    explain_sql = f"""\
EXPLAIN (
    ANALYZE,
    VERBOSE,
    BUFFERS,
    WAL,
    SETTINGS,
    FORMAT JSON
)
{sql}
"""
    with psycopg.connect(PG_CONNINFO) as conn:
        conn.set_read_only(True)
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            rows = cur.fetchall()
    return json.dumps(rows[0][0], indent=2)


def get_pg_stat_statements() -> list[QueryGroup]:
    sql = """
SELECT query, queryid, calls, total_exec_time, mean_exec_time, max_exec_time
FROM pg_stat_statements;
    """
    with psycopg.connect(PG_CONNINFO) as conn:
        conn.set_read_only(True)
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    query_groups = []
    for query, query_id, calls, total_exec_time, mean_exec_time, max_exec_time in rows:
        query_groups.append(
            QueryGroup(
                query_id=query_id,
                sql=query,
                plan="",
                count=calls,
                total_runtime_ms=total_exec_time,
                avg_runtime_ms=mean_exec_time,
                max_runtime_ms=max_exec_time,
            )
        )

    return query_groups


@lru_cache(maxsize=CACHE_SIZE)
def send_for_analysis(query: SlowQuery | QueryGroup) -> Tuple[str, str]:
    password = ""  # FIXME?
    r = requests.post(
        API_URL + "/new.json",
        json={"query": format_sql(query.sql), "plan": query.plan, "password": password},
    )
    r.raise_for_status()
    delete_key = r.json().get("deleteKey")
    plan_id = r.json().get("id")
    url = API_URL + "/plan/" + plan_id
    delete_url = url + "/" + delete_key
    return (url, delete_url)


@lru_cache(maxsize=CACHE_SIZE)
def format_sql(sql: str, inline=False) -> str:
    # Huge queries can cause pglast or textual to freeze, so we limit the size
    # of the query to 10000 characters
    shortened = False
    tmp = re.sub(r"\([0-9, ]{1000,}\)", '("<...>")', sql)
    if tmp != sql:
        shortened = True
        sql = tmp
    if len(sql) > 100000:
        return sql[:100000]
    preserve_comments = not inline
    try:
        pretty = pglast.prettify(
            sql,
            comma_at_eoln=True,
            semicolon_after_last_statement=True,
            preserve_comments=preserve_comments,
        )
        if inline:
            return " ".join(pretty.split())
        elif shortened:
            return "/* WARNING: Query shortened due to extreme length */\n" + pretty
        else:
            return pretty
    except pglast.Error:
        return sql


def shorten_str(text: str, max_length: int = 80) -> str:
    text = text.replace("\n", " ")

    if len(text) <= max_length:
        return text

    return text[: max_length - 1] + "…"


def extract_runtime_ms(message: str) -> float | None:
    if not message.startswith("duration: "):
        return None

    try:
        duration_part = message.split("ms", 1)[0]
        return float(duration_part.split()[1])
    except (IndexError, ValueError):
        return None


def extract_query(message: str) -> str:
    lines = message.splitlines()
    if len(lines) < 2:
        return ""
    if not lines[1].startswith("Query Text: "):
        return ""
    sql = lines[1].replace("Query Text: ", "").strip()
    return sql


def extract_plan(message: str) -> str:
    lines = message.splitlines()
    if len(lines) < 2:
        return ""
    sql = "\n".join(lines[2:]).strip()
    return sql


def extract_regex(regex: re.Pattern, sql: str) -> str:
    match = regex.search(sql)
    if match:
        # Return first capturing group that is not None
        for group in match.groups():
            if group is not None:
                return group
    return "-"


def find_latest_log() -> Path:
    files = sorted(
        LOG_DIR.glob("postgresql-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not files:
        raise FileNotFoundError(f"No log files found at {LOG_DIR}")

    return files[0]


def aggregate(set_: set, aggregation_mode: str) -> int | str:
    if aggregation_mode == "count":
        return len({e for e in set_ if e != "-"})
    elif aggregation_mode == "sample":
        return next(iter(set_), "-")
    elif aggregation_mode == "list":
        return ", ".join(sorted(set_))
    else:
        raise ValueError(f"Unknown aggregation type: {aggregation_mode}")


def group_queries(
    queries: list[SlowQuery],
    group_by: str = "query_id",
) -> list[QueryGroup]:
    groups: dict[str, list[SlowQuery]] = defaultdict(list)

    for query in queries:
        if group_by == "query_id":
            groups[query.query_id].append(query)
        else:
            groups[query.extra_fields[group_by]].append(query)

    result: list[QueryGroup] = []

    for group, items in groups.items():
        runtimes = [q.runtime_ms for q in items]

        # Aggregate fields
        extra_fields = {}
        for extra_field in EXTRA_FIELDS:
            if group_by == extra_field["key"]:
                extra_fields[extra_field["key"]] = group
            else:
                aggregation_mode = extra_field.get("aggregation", "count")
                key = extra_field["key"]
                extra_fields[extra_field["key"]] = str(
                    aggregate(
                        {q.extra_fields.get(key, "-") for q in items}, aggregation_mode
                    )
                )

        if group_by == "query_id":
            query_id = group
        else:
            query_id = str(aggregate({q.query_id for q in items}, "count"))

        result.append(
            QueryGroup(
                query_id=query_id,
                sql=items[0].sql,
                plan=items[0].plan,
                count=len(items),
                total_runtime_ms=sum(runtimes),
                avg_runtime_ms=sum(runtimes) / len(runtimes),
                max_runtime_ms=max(runtimes),
                extra_fields=extra_fields,
            )
        )

    return result


def parse_log_file(path: Path) -> list[SlowQuery]:
    result: list[SlowQuery] = []

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = entry.get("message")
            if not isinstance(message, str):
                continue
            if not message.startswith("duration: "):
                continue

            runtime_ms = extract_runtime_ms(message)
            if runtime_ms is None:
                continue

            sql = extract_query(message)
            plan = extract_plan(message)
            extra_fields = {
                field["key"]: extract_regex(field["regex"], sql)
                for field in EXTRA_FIELDS
            }

            result.append(
                SlowQuery(
                    time=str(entry.get("timestamp", "")),
                    runtime_ms=runtime_ms,
                    ps=str(entry.get("ps", "")),
                    sql=sql,
                    plan=plan,
                    query_id=str(entry.get("query_id", "-")),
                    extra_fields=extra_fields,
                )
            )

    return result


def parse_log_files(paths: list[Path]) -> list[SlowQuery]:
    result: list[SlowQuery] = []
    for path in paths:
        result.extend(parse_log_file(path))
    return result


class FilterScreen(Screen):
    """Dialog for selecting a filter."""

    CSS = """
    #dialog {
        width: 60;
        height: 21;
        border: round $accent;
    }
    FilterScreen {
        background: $surface 90%;
        align: center middle;
        content-align: center middle;
    }

    #title {
        text-style: bold;
    }

    RadioSet {
        height: auto;
    }

    Input {
        width: 100%;
    }

    Button {
        width: 100%;
    }
    """

    def __init__(self, column_list: list[str]):
        super().__init__()
        self.column_list = column_list
        self.radio_buttons = []
        self.modes = [
            RadioButton("substring", value=True),
            RadioButton("exact"),
            RadioButton("regex"),
        ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Filter Table", id="title"),
            Select.from_values(self.column_list, id="column", value="sql"),
            Input(
                placeholder="Type filter text…",
                id="filter",
            ),
            RadioSet(*self.modes, id="mode"),
            Checkbox("Inverse", id="inverse"),
            Button("Apply Filter", variant="primary", id="apply"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.apply_filter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.apply_filter()

    def apply_filter(self) -> None:
        column = self.query_one("#column")
        input_widget = self.query_one("#filter")
        mode = self.query_one("#mode")
        inverse = self.query_one("#inverse")

        self.app.filter(
            column=column.value,
            filter_str=input_widget.value,
            filter_mode=mode.pressed_button.label.plain,
            inverse=inverse.value,
        )


class SlowQueryApp(App[None]):
    details_height = 15
    CSS = """
    #layout {
        grid-size: 1 2;
        grid-rows: 1fr 15;
    }

    #table {
        row-span: 1;
    }

    #details {
        border: solid $accent;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("a", "explain", "Analyse"),
        ("A", "explain_analyze", "Run and Analyse"),
        ("+", "increase_details", "Increase Detail View"),
        ("-", "decrease_details", "Decrease Detail View"),
        ("g", "toggle_grouping", "Toggle grouping"),
        ("s", "switch_sort_column", "Switch Sort Column"),
        ("r", "toggle_sort_reverse", "Toggle Sort Order"),
        ("c", "copy_query", "Copy Query to Clipboard"),
        ("f", "filter", "Filter"),
    ]

    def __init__(
        self,
        queries: list[SlowQuery],
        grouped_queries: Optional[dict[str, list[QueryGroup]]] = None,
        grouped_by=None,
    ) -> None:
        super().__init__()
        self.queries = queries
        self.grouped_by = grouped_by
        if grouped_queries:
            self.grouped_queries = grouped_queries
        else:
            self.grouped_queries = {}
        # If using pg_stat_statements the queries are always grouped by query_id
        if queries == [] and grouped_by is not None:
            self.available_groupings: list[str | None] = ["query_id"]
            self.sort_column = 3
        else:
            self.available_groupings = [None, "query_id"]
            self.available_groupings += [field["key"] for field in EXTRA_FIELDS]
            self.sort_column = -1
        self.sort_reverse = True
        self.selected_row = 0
        self.filtered = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid(id="layout"):
            yield DataTable(id="table")
            yield TextArea.code_editor(
                "",
                id="details",
                read_only=True,
                highlight_cursor_line=False,
                language="sql",
            )
        yield Footer(compact=True)

    def get_selected_query(self) -> SlowQuery | QueryGroup | None:
        table = self.query_one(DataTable)
        if table.cursor_row < 0:
            return None
        try:
            if self.grouped_by:
                return self.grouped_queries[self.grouped_by][self.selected_row]
            return self.queries[self.selected_row]
        except IndexError:
            return None

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        if event.row_key.value is None:
            raise ValueError("Row key invalid")
        try:
            self.selected_row = int(event.row_key.value)
        except ValueError:
            raise ValueError("Row key invalid")
        self.update_details()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if self.sort_column == event.column_index:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = event.column_index
        self.update_sort()

    def update_details(self) -> None:
        query = self.get_selected_query()
        if query is None:
            return
        details = self.query_one("#details", TextArea)
        details.load_text(format_sql(query.sql) + "\n\n" + query.plan)

    def update_sort(self) -> None:
        table = self.query_one(DataTable)
        if self.sort_column >= 0:
            sort_col_key = ColumnKey(self.columns[self.sort_column]["key"])
            sort_col_str = self.columns[self.sort_column]["display_name"]
            for col in self.columns:
                key = ColumnKey(col["key"])
                name = col["display_name"]
                table.columns[key].label = Text(name)
            table.columns[sort_col_key].label = Text(
                sort_col_str + (" ▼" if self.sort_reverse else " ▲")
            )

            def natural_sort(input):
                if isinstance(input, Text):
                    input = input.plain
                if isinstance(input, Number):
                    input = input.value
                return input

            table.sort(sort_col_key, key=natural_sort, reverse=self.sort_reverse)
            table.refresh()
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            self.selected_row = int(cell_key[0].value or 0)
            self.update_details()

    def action_filter(self) -> None:
        self.push_screen(FilterScreen([c["key"] for c in self.columns]))

    def filter(
        self, column: str, filter_str: str, filter_mode: str, inverse: bool
    ) -> None:
        table = self.query_one(DataTable)
        # If it was already filtered, we first recreate the original
        if self.filter:
            table.clear(columns=True)
            self.on_mount()
        to_remove = []
        for row_key, row in table.rows.items():
            value = table.get_cell(row_key, column).plain
            if filter_text(
                value,
                filter_str=filter_str,
                filter_mode=filter_mode,
                inverse=inverse,
            ):
                to_remove.append(row_key)
        for row_key in to_remove:
            table.remove_row(row_key)
        self.pop_screen()
        self.filtered = True

    def action_copy_query(self) -> None:
        query = self.get_selected_query()

        if query is None:
            self.notify("No query selected")
            return

        osc52_copy(format_sql(query.sql), driver=self.app._driver)
        self.notify("Query copied to clipboard")

    def action_switch_sort_column(self) -> None:
        self.sort_column = (self.sort_column + 1) % len(self.columns)
        self.update_sort()

    def action_toggle_sort_reverse(self) -> None:
        self.sort_reverse = not self.sort_reverse
        self.update_sort()

    def action_explain(self) -> None:
        query = self.get_selected_query()
        if query is None:
            self.notify("No query selected")
            return
        try:
            result = send_for_analysis(query)

            self.notify(
                "Successfully sent: " + result[0],
                severity="information",
                timeout=20,
            )
            # open the url in the browser – this sadly doesn't work via ssh
            self.open_url(result[0])
        except Exception as exc:
            self.notify(
                f"Error: {exc}",
                severity="error",
            )

    def action_explain_analyze(self) -> None:
        query = self.get_selected_query()

        if query is None:
            self.notify("No query selected")
            return

        if isinstance(query, SlowQuery) and query.ps != "SELECT":
            self.notify(
                "Only SELECT queries can be safely run with EXPLAIN ANALYZE",
                severity="warning",
            )
            return
        try:
            query.plan = run_explain_analyze(query.sql)
            result = send_for_analysis(query)

            self.notify(
                "Successfully executed and sent: " + result[0],
                severity="information",
            )

        except Exception as exc:
            self.notify(
                f"Error: {exc}",
                severity="error",
            )

    def action_toggle_grouping(self) -> None:
        self.grouped_by = self.available_groupings[
            (self.available_groupings.index(self.grouped_by) + 1)
            % len(self.available_groupings)
        ]
        self.clear_notifications()
        self.notify(f"Grouped by: {self.grouped_by}")
        table = self.query_one(DataTable)
        table.clear(columns=True)
        self.on_mount()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)

        table.cursor_type = "row"
        table.zebra_stripes = True
        width_sql_column = self.viewport_size[0] - 100 - 20 * len(EXTRA_FIELDS)
        # Handle small terminals
        if self.viewport_size[1] < 25:
            layout = self.query_one("#layout")
            layout.styles.grid_rows = (
                layout.styles.grid_rows[0],
                5,
            )

        if self.grouped_by:
            if self.grouped_by not in self.grouped_queries:
                self.grouped_queries[self.grouped_by] = group_queries(
                    self.queries, self.grouped_by
                )
            self.columns = COLUMNS_GROUPED
            for c in COLUMNS_GROUPED:
                key = c["key"]
                if key != self.grouped_by and c.get("aggregation") == "count":
                    name = "# " + c["name"]
                elif key != self.grouped_by and c.get("aggregation") == "sample":
                    name = "Sample " + c["name"]
                else:
                    name = c["name"]
                c["display_name"] = name
                # Hack: Add two spaces to the end of the name to make sure the
                # column is wide enough for the sort indicator (▼ or ▲)
                name_ = name + "  "
                table.add_column(Text(name_), key=c["key"])
                table.columns[ColumnKey(c["key"])].label = Text(name)

            for index, group in enumerate(self.grouped_queries[self.grouped_by]):
                table.add_row(
                    Number(group.count),
                    Number(group.avg_runtime_ms),
                    Number(group.max_runtime_ms),
                    Number(group.total_runtime_ms),
                    Text(str(group.query_id)),
                    Text(
                        shorten_str(
                            format_sql(group.sql, inline=True),
                            max_length=width_sql_column,
                        )
                    ),
                    *[
                        Text(str(group.extra_fields.get(field["key"], "-")))
                        for field in EXTRA_FIELDS
                    ],
                    key=str(index),
                )

        else:
            self.columns = COLUMNS_PLAIN
            for c in COLUMNS_PLAIN:
                c["display_name"] = c["name"]
                # Hack: Add two spaces to the end of the name to make sure the
                # column is wide enough for the sort indicator (▼ or ▲)
                name_ = c["name"] + "  "
                table.add_column(Text(name_), key=c["key"])
                table.columns[ColumnKey(c["key"])].label = Text(c["name"])

            for index, query in enumerate(self.queries):
                table.add_row(
                    Text(query.time),
                    Number(query.runtime_ms),
                    Text(query.ps),
                    Text(
                        shorten_str(
                            format_sql((query.sql), inline=True),
                            max_length=width_sql_column,
                        )
                    ),
                    Text(query.query_id),
                    *[
                        Text(query.extra_fields.get(field["key"], "-"))
                        for field in EXTRA_FIELDS
                    ],
                    key=str(index),
                )
        self.update_sort()

    def action_increase_details(self) -> None:
        grid = self.query_one("#layout")
        self.details_height += 1
        grid.styles.grid_rows = "1fr " + str(self.details_height)

    def action_decrease_details(self) -> None:
        grid = self.query_one("#layout")
        self.details_height -= 1
        grid.styles.grid_rows = "1fr " + str(self.details_height)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """Disable keybindings that won't work anyway."""
        if action in ["explain_analyze", "copy_query"]:
            query = self.get_selected_query()
            if query is None:
                return False
        if action == "explain":
            query = self.get_selected_query()
            if not isinstance(query, SlowQuery) or not query.plan:
                return False
        if action == "toggle_grouping":
            if len(self.available_groupings) <= 1:
                return False
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=APP_NAME, description="PostgreSQL Slow Query Analyzer"
    )

    parser.add_argument(
        "-g",
        "--grouped",
        action="store_true",
        help="Group Queries by query_id",
    )
    parser.add_argument(
        "-s",
        "--stat_statements",
        action="store_true",
        help="Load queries from pg_stat_statements instead of logfiles",
    )
    parser.add_argument(
        "logfiles",
        nargs="*",
        type=Path,
        help="Path to PostgreSQL log files to analyse"
        + " (default: latest log file in /var/log/postgresql/)",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args()

    if args.stat_statements and args.logfiles:
        parser.error("Cannot use --stat_statements and logfiles together")

    if args.stat_statements:
        grouped_queries = get_pg_stat_statements()
        app = SlowQueryApp(
            queries=[],
            grouped_queries={"query_id": grouped_queries},
            grouped_by="query_id",
        )
    else:
        logfiles = args.logfiles or [find_latest_log()]
        queries = parse_log_files(logfiles)
        group_by = "query_id" if args.grouped else None
        app = SlowQueryApp(queries, grouped_by=group_by)

    app.run()


if __name__ == "__main__":
    main()
