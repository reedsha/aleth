"""Type stubs for the compiled core (`crates/deepagents_core`).

`deepagents_core` is a PyO3 extension module: at runtime it is a `.pyd` the type
checker cannot read, so every attribute lookup on it is reported as unknown. This
stub is the declaration of that boundary -- what the Rust core promises the Python
side, in the same order as `crates/deepagents_core/src/lib.rs`.

The vocabulary JSON the parser functions take is built by
`tools.plan_parser._vocabulary_json()`: a mapping of upper-cased tag or legacy alias
to its canonical spelling, the canonical UI tag, and Laya's UI keyword pattern.

Functions returning a JSON string return either a payload or an envelope; the
envelopes are documented on the function.
"""

__version__: str

#: The inline Behavioral Ledger marker the compiler writes and the parser reads back.
BEHAVIORAL_LOG_PREFIX: str

# --- Plan parsing -----------------------------------------------------------

def parse_markdown_to_plan_dict(
    content: str, filename: str, relaxed: bool, vocabulary_json: str
) -> str:
    """Strict plan dictionary as JSON."""

def compile_plan_json_to_markdown(plan_json: str, vocabulary_json: str) -> str:
    """Standardized markdown for a plan dictionary."""

def check_plan_structure(content: str) -> str:
    """`{"structured", "issues", "summary", "counts"}` as JSON."""

def parse_plan_tree(content: str, filename: str, vocabulary_json: str) -> str:
    """The plan dictionary's `steps` list as a JSON array."""

def explicit_tags(content: str, vocabulary_json: str) -> str:
    """`{clean title: tag}` for the tags the markdown itself carries, as JSON."""

def explicit_ui_titles(content: str, vocabulary_json: str) -> str:
    """Titles carrying a literal `[UI]` tag, as a JSON array."""

def parse_deliverables(text: str) -> list[str]: ...
def format_deliverables(files: list[str]) -> str: ...

# --- Plan state -------------------------------------------------------------

def prepare_plan_state(plan_json: str, active_plan: str) -> str:
    """`{"ok": true, "plan": {...}}` or `{"ok": false, "error": "..."}`.

    Recomputes `steps`, `metrics`, `updated_at` and `plan_file`; writes nothing.
    """

def update_plan_task(plan_json: str, task_id: str, new_status: str,
                     detail_note: str | None, files: list[str]) -> str:
    """`{"found": bool, "plan": {...}}`; no write is performed."""

def append_pending_task(
    plan_json: str, title: str, note: str | None, tag: str | None
) -> str:
    """`{"ok": bool, "plan": {...}, "task": {...}}`."""

def read_plan_markdown(plan_md_path: str) -> str:
    """The markdown as text, or `""` when it is not on disk."""

def write_plan_markdown(plan_md_path: str, content: str) -> str:
    """`{"ok": bool, "path": str}` / `{"ok": false, "error": str}`."""

def plan_structure_report(content: str) -> str:
    """The AST structure verdict as JSON."""

# --- Token-budgeted truncation ----------------------------------------------

def truncate_for_read_file(content: str, filename: str, max_chars: int) -> str:
    """The read, middle-truncated unless it fits or is a `.md`/`.json` file."""

def truncate_shell_output(output: str, max_chars: int) -> str:
    """The output, middle-truncated to keep its head and its exit trace."""

def truncate_test_output(output: str, max_chars: int) -> str:
    """A test runner's output, middle-truncated to keep its summary line."""
