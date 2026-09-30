//! Compiled core engine for DeepAgents Studio.
//!
//! Three concerns live here, all of them on the hot path between the UI and the
//! plan on disk:
//!
//! * [`parser`] -- markdown plan text to strict plan dictionary, and back.
//! * [`state`] -- plan-state canonicalisation and the atomic markdown projection
//!   write, under an OS-level lock. (State itself lives in SQLite on the Python side;
//!   markdown is never read back to determine it.)
//! * [`text`] -- token-budgeted truncation of file reads, shell output and test output.
//!
//! The Python side keeps what it is good at -- paths, policy and process state --
//! and calls in here for the work that has to be exact, fast and race-free. Every
//! function on this boundary is a pure translation of text or JSON, so the module
//! can be exercised without a window, a workspace or an agent.

use pyo3::prelude::*;

pub mod parser;
pub mod state;
pub mod text;
pub mod tokenizer;

use parser::Vocabulary;

/// Rebuilds the tag vocabulary from the JSON the Python side owns.
///
/// The vocabulary deliberately does *not* live here: `tools/task_tags.py` is the
/// single definition of the project's tag language, and it is read by the UI and
/// the delegation path as well as by this parser. Passing it in means the plan tree
/// and the delegation path cannot drift about what a tag is.
fn vocabulary_from_json(json: &str) -> PyResult<Vocabulary> {
    serde_json::from_str(json)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(format!("bad vocabulary: {}", error)))
}

fn parse_json(json: &str) -> PyResult<serde_json::Value> {
    serde_json::from_str(json)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(format!("bad json: {}", error)))
}

fn dump_json(value: &serde_json::Value) -> PyResult<String> {
    serde_json::to_string(value)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(format!("could not serialize: {}", error)))
}

// ---------------------------------------------------------------------------
// Plan parsing
// ---------------------------------------------------------------------------

/// Parses markdown plan text into a strict plan dictionary, as JSON.
#[pyfunction]
fn parse_markdown_to_plan_dict(
    content: &str,
    filename: &str,
    relaxed: bool,
    vocabulary_json: &str,
) -> PyResult<String> {
    let vocabulary = vocabulary_from_json(vocabulary_json)?;
    let plan = parser::parse_markdown_to_plan_dict(content, filename, relaxed, &vocabulary);
    dump_json(&plan)
}

/// Compiles a strict plan dictionary back into standardized markdown.
#[pyfunction]
fn compile_plan_json_to_markdown(plan_json: &str, vocabulary_json: &str) -> PyResult<String> {
    let vocabulary = vocabulary_from_json(vocabulary_json)?;
    let plan = parse_json(plan_json)?;
    Ok(parser::compile_plan_json_to_markdown(&plan, &vocabulary))
}

/// Verdict on whether a document carries the plan AST the parser can read, as JSON.
#[pyfunction]
fn check_plan_structure(content: &str) -> PyResult<String> {
    dump_json(&parser::check_plan_structure(content))
}

/// The plan dictionary's own `steps` list, parsed from markdown, as JSON.
#[pyfunction]
fn parse_plan_tree(content: &str, filename: &str, vocabulary_json: &str) -> PyResult<String> {
    let vocabulary = vocabulary_from_json(vocabulary_json)?;
    dump_json(&parser::parse_plan_tree(content, filename, &vocabulary))
}

/// Every leading tag the markdown actually carries, as a JSON object.
#[pyfunction]
fn explicit_tags(content: &str, vocabulary_json: &str) -> PyResult<String> {
    let vocabulary = vocabulary_from_json(vocabulary_json)?;
    dump_json(&serde_json::to_value(parser::explicit_tags(content, &vocabulary)).unwrap_or_default())
}

/// Titles carrying a literal `[UI]` tag, as a JSON array.
#[pyfunction]
fn explicit_ui_titles(content: &str, vocabulary_json: &str) -> PyResult<String> {
    let vocabulary = vocabulary_from_json(vocabulary_json)?;
    dump_json(&serde_json::to_value(parser::explicit_ui_titles(content, &vocabulary)).unwrap_or_default())
}

/// File paths declared by a `Files: a.py, b.py` style detail line.
#[pyfunction]
fn parse_deliverables(text: &str) -> Vec<String> {
    parser::parse_deliverables(text)
}

/// Renders a files list as the detail line `parse_deliverables` reads back.
#[pyfunction]
fn format_deliverables(files: Vec<String>) -> String {
    parser::format_deliverables(&files)
}

// ---------------------------------------------------------------------------
// Plan state (canonicalisation, task edits, the markdown projection)
// ---------------------------------------------------------------------------

/// Derives the canonical state that must be stored.
///
/// Returns `{"ok": true, "plan": {...}}`. The caller commits it to the SQLite store and
/// then renders the markdown projection from it.
#[pyfunction]
fn prepare_plan_state(plan_json: &str, active_plan: &str) -> PyResult<String> {
    let mut plan = parse_json(plan_json)?;
    match state::prepare_plan_state(&mut plan, active_plan) {
        Ok(()) => dump_json(&serde_json::json!({"ok": true, "plan": plan})),
        Err(error) => dump_json(&serde_json::json!({"ok": false, "error": error})),
    }
}

/// Updates one task's status in memory: `{"found": true, "plan": {...}}`.
#[pyfunction]
fn update_plan_task(
    plan_json: &str,
    task_id: &str,
    new_status: &str,
    detail_note: Option<&str>,
    files: Vec<String>,
) -> PyResult<String> {
    let mut plan = parse_json(plan_json)?;
    let found = state::update_plan_task(&mut plan, task_id, new_status, detail_note, &files);
    dump_json(&serde_json::json!({"found": found, "plan": plan}))
}

/// Adds a pending task to the plan's last section:
/// `{"ok": true, "plan": {...}, "task": {...}}`.
#[pyfunction]
fn append_pending_task(
    plan_json: &str,
    title: &str,
    note: Option<&str>,
    tag: Option<&str>,
) -> PyResult<String> {
    let mut plan = parse_json(plan_json)?;
    match state::append_pending_task(&mut plan, title, note, tag) {
        Ok(task) => dump_json(&serde_json::json!({"ok": true, "plan": plan, "task": task})),
        Err(error) => dump_json(&serde_json::json!({"ok": false, "error": error})),
    }
}

/// The active plan's markdown as text, or `""` when it is not on disk.
#[pyfunction]
fn read_plan_markdown(plan_md_path: &str) -> String {
    state::read_plan_markdown(plan_md_path)
}

/// Writes the active plan's markdown, atomically: `{"ok": true, "path": "..."}`.
#[pyfunction]
fn write_plan_markdown(plan_md_path: &str, content: &str) -> PyResult<String> {
    match state::write_plan_markdown(plan_md_path, content) {
        Ok(path) => dump_json(&serde_json::json!({"ok": true, "path": path})),
        Err(error) => dump_json(&serde_json::json!({"ok": false, "error": error})),
    }
}

/// The AST structure verdict for a plan document, as JSON.
#[pyfunction]
fn plan_structure_report(content: &str) -> PyResult<String> {
    dump_json(&state::plan_structure_report(content))
}

// ---------------------------------------------------------------------------
// Context slicing
// ---------------------------------------------------------------------------

/// A workspace file read, middle-truncated when it would overflow the budget.
#[pyfunction]
fn truncate_for_read_file(content: &str, filename: &str, max_chars: usize) -> String {
    text::truncate_for_read_file(content, filename, max_chars)
}

/// A command's output, middle-truncated to keep its head and its exit trace.
#[pyfunction]
fn truncate_shell_output(output: &str, max_chars: usize) -> String {
    text::truncate_shell_output(output, max_chars)
}

/// A test runner's output, middle-truncated to keep its head and its summary line.
#[pyfunction]
fn truncate_test_output(output: &str, max_chars: usize) -> String {
    text::truncate_test_output(output, max_chars)
}

#[pymodule]
fn deepagents_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("BEHAVIORAL_LOG_PREFIX", parser::BEHAVIORAL_LOG_PREFIX)?;

    module.add_function(wrap_pyfunction!(parse_markdown_to_plan_dict, module)?)?;
    module.add_function(wrap_pyfunction!(compile_plan_json_to_markdown, module)?)?;
    module.add_function(wrap_pyfunction!(check_plan_structure, module)?)?;
    module.add_function(wrap_pyfunction!(parse_plan_tree, module)?)?;
    module.add_function(wrap_pyfunction!(explicit_tags, module)?)?;
    module.add_function(wrap_pyfunction!(explicit_ui_titles, module)?)?;
    module.add_function(wrap_pyfunction!(parse_deliverables, module)?)?;
    module.add_function(wrap_pyfunction!(format_deliverables, module)?)?;

    module.add_function(wrap_pyfunction!(prepare_plan_state, module)?)?;
    module.add_function(wrap_pyfunction!(update_plan_task, module)?)?;
    module.add_function(wrap_pyfunction!(append_pending_task, module)?)?;
    module.add_function(wrap_pyfunction!(read_plan_markdown, module)?)?;
    module.add_function(wrap_pyfunction!(write_plan_markdown, module)?)?;
    module.add_function(wrap_pyfunction!(plan_structure_report, module)?)?;

    module.add_function(wrap_pyfunction!(truncate_for_read_file, module)?)?;
    module.add_function(wrap_pyfunction!(truncate_shell_output, module)?)?;
    module.add_function(wrap_pyfunction!(truncate_test_output, module)?)?;

    Ok(())
}
