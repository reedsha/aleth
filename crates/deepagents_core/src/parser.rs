//! Markdown <-> plan-dictionary translation, ported from `tools/plan_parser.py`.
//!
//! Pure translation: markdown text in, strict plan dictionary out (and back). No
//! file I/O, no state. The vocabulary this parser tags titles with is *not* owned
//! here -- it arrives as a [`Vocabulary`], built by the Python side from
//! `tools/task_tags.py` and `orchestration/workflow/templates.py`, so the plan
//! tree and the delegation path cannot drift about what a tag is.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use regex::Regex;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::tokenizer::{tokenize, Kind, Token};

/// The marker the compiler writes and the parser reads back for an inline ledger
/// entry. Defined once here for the same reason it was defined once in Python: a
/// second spelling would stop the parser recognising it, and the log would
/// re-accumulate as an ordinary detail note on every round-trip.
pub const BEHAVIORAL_LOG_PREFIX: &str = "\u{1F7E2} Behavioral Log:";

const STATE_SUMMARY_TITLE: &str = "\u{1F30D} Global State Summary";

/// A task's status as its checkbox mark spells it.
const MARK_FOR_STATUS: [(&str, &str); 3] =
    [("completed", "x"), ("in_progress", "-"), ("failed", "!")];

/// The tag vocabulary and the UI keyword pattern, both owned by Python.
#[derive(Debug, Clone, Deserialize)]
pub struct Vocabulary {
    /// Upper-cased tag or legacy alias -> canonical spelling, e.g. `UI` -> `FE`.
    pub canonical: HashMap<String, String>,
    /// The canonical spelling of the UI tag.
    pub ui_tag: String,
    /// Laya's UI keyword pattern, e.g. `\b(?:ui|frontend|interface|view)s?\b`.
    pub ui_keyword_pattern: String,
}

/// Every regular expression the translation needs.
///
/// Compiled once per process, not once per call: the parser runs on every plan render
/// (and on every rehydration), and compiling six patterns each time was most of a
/// millisecond of the work. Only the UI keyword pattern is the caller's, so it is
/// cached per distinct pattern.
struct Patterns {
    deliverables: Regex,
    behavioral_log: Regex,
    checkbox: Regex,
    state_summary: Regex,
    leading_tag: Regex,
    ui_keyword: Regex,
}

/// The patterns that do not depend on the caller's vocabulary.
struct FixedPatterns {
    deliverables: Regex,
    behavioral_log: Regex,
    checkbox: Regex,
    state_summary: Regex,
    leading_tag: Regex,
    /// A `## [TAG] Title` heading: a logged task rather than a section.
    task_header: Regex,
    /// The bullet or number a relaxed list item opens with.
    bullet_prefix: Regex,
    title_line: Regex,
    section_line: Regex,
    any_bullet: Regex,
}

impl FixedPatterns {
    fn compile() -> Self {
        FixedPatterns {
            deliverables: deliverables_pattern(),
            behavioral_log: Regex::new(&format!(
                r"^\s*{}\s*(.*)$",
                regex::escape(BEHAVIORAL_LOG_PREFIX)
            ))
            .expect("behavioral log pattern"),
            // The fourth mark, `!`, is a task that ran and did not pass verification.
            checkbox: Regex::new(r"^[*-]?\s*\[([ xX\-!])\]\s*(.*)").expect("checkbox pattern"),
            state_summary: Regex::new(r"(?i)global\s+state\s+summary").expect("summary pattern"),
            leading_tag: Regex::new(r"^\[([^\[\]]+)\](?:[ \t]+(.*))?$").expect("tag pattern"),
            task_header: Regex::new(r"^\[([^\[\]]+)\]").expect("task header pattern"),
            bullet_prefix: Regex::new(r"^(?:[*-]|\d+[.)])\s*").expect("bullet prefix pattern"),
            title_line: Regex::new(r"^\s*#\s+\S").expect("title pattern"),
            section_line: Regex::new(r"^\s*#{2,3}\s+\S").expect("section pattern"),
            any_bullet: Regex::new(r"^\s*(?:[*-]|\d+[.)])\s+\S").expect("bullet pattern"),
        }
    }
}

fn fixed_patterns() -> &'static FixedPatterns {
    static FIXED: std::sync::OnceLock<FixedPatterns> = std::sync::OnceLock::new();
    FIXED.get_or_init(FixedPatterns::compile)
}

/// Laya's keyword pattern, cached per distinct pattern string.
fn ui_keyword_pattern(pattern: &str) -> Regex {
    static CACHE: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<String, Regex>>> =
        std::sync::OnceLock::new();
    let cache = CACHE.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()));
    let mut cache = cache.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    cache
        .entry(pattern.to_string())
        .or_insert_with(|| Regex::new(pattern).unwrap_or_else(|_| Regex::new(r"\bui\b").expect("fallback")))
        .clone()
}

impl Patterns {
    fn new(vocabulary: &Vocabulary) -> Self {
        let fixed = fixed_patterns();
        Patterns {
            deliverables: fixed.deliverables.clone(),
            behavioral_log: fixed.behavioral_log.clone(),
            checkbox: fixed.checkbox.clone(),
            state_summary: fixed.state_summary.clone(),
            leading_tag: fixed.leading_tag.clone(),
            ui_keyword: ui_keyword_pattern(&vocabulary.ui_keyword_pattern),
        }
    }
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/// Python's `str.splitlines()` for the line shapes a markdown document uses.
fn split_lines(text: &str) -> Vec<&str> {
    text.split('\n')
        .map(|line| line.strip_suffix('\r').unwrap_or(line))
        .collect()
}

fn as_array(value: Option<&Value>) -> &[Value] {
    match value {
        Some(Value::Array(items)) => items,
        _ => &[],
    }
}

fn as_str_or<'a>(value: Option<&'a Value>, default: &'a str) -> &'a str {
    match value {
        Some(Value::String(text)) => text,
        _ => default,
    }
}

fn non_empty_str(value: Option<&Value>) -> Option<&str> {
    match value {
        Some(Value::String(text)) if !text.is_empty() => Some(text),
        _ => None,
    }
}

/// The canonical spelling of a tag, or the tag itself when it is not in the
/// vocabulary -- `tag_prefix`'s rule in `tools/task_tags.py`.
fn tag_prefix(vocabulary: &Vocabulary, tag: &str) -> String {
    let canonical = vocabulary
        .canonical
        .get(&tag.to_uppercase())
        .cloned()
        .unwrap_or_else(|| tag.to_string());
    format!("[{}] ", canonical)
}

fn is_tag(vocabulary: &Vocabulary, name: &str) -> bool {
    vocabulary.canonical.contains_key(&name.to_uppercase())
}

/// Splits a leading bracketed tag off a title. The port of
/// `tools/task_tags.split_tag`.
fn split_leading_tag<'a>(patterns: &Patterns, vocabulary: &Vocabulary, title: &'a str) -> (String, Option<String>) {
    let Some(captures) = patterns.leading_tag.captures(title) else {
        return (title.to_string(), None);
    };
    let raw = captures.get(1).map(|m| m.as_str().trim()).unwrap_or("");
    let Some(canonical) = vocabulary.canonical.get(&raw.to_uppercase()) else {
        return (title.to_string(), None);
    };
    let rest = captures.get(2).map(|m| m.as_str()).unwrap_or("");
    (rest.trim().to_string(), Some(canonical.clone()))
}

/// The `[UI]` token as a standalone word: bracketed, and separated from whatever
/// surrounds it.
///
/// The Python original is a lookahead -- `(?:^|\s)\[ui\](?=\s|$)` -- which the
/// `regex` crate cannot express, so the two conditions are checked by hand. The
/// preceding whitespace is part of the match (and so is removed with it), exactly
/// as Python's substitution removed it, while the following character is only
/// inspected.
fn find_ui_token(text: &str) -> Option<(usize, usize)> {
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut i = 0usize;
    while i + 4 <= chars.len() {
        let window: String = chars[i..i + 4].iter().map(|(_, c)| *c).collect();
        if !window.eq_ignore_ascii_case("[ui]") {
            i += 1;
            continue;
        }
        let before_ok = i == 0 || chars[i - 1].1.is_whitespace();
        let after_ok = chars.get(i + 4).map(|(_, c)| c.is_whitespace()).unwrap_or(true);
        if before_ok && after_ok {
            let match_start = if i == 0 { chars[0].0 } else { chars[i - 1].0 };
            let end = chars.get(i + 4).map(|(byte, _)| *byte).unwrap_or(text.len());
            return Some((match_start, end));
        }
        i += 1;
    }
    None
}

/// Whether a title should carry the UI tag on the strength of its wording.
fn inferred_ui(patterns: &Patterns, title: &str) -> bool {
    let prose = replace_ui_literals(title);
    patterns.ui_keyword.is_match(&prose.to_lowercase())
}

/// Neutralises the `[UI]` literal wherever it appears, so a task that merely
/// *mentions* the tag is not tagged on the strength of the literal. Replaced by a
/// space rather than deleted, so the surrounding words stay apart.
fn replace_ui_literals(title: &str) -> String {
    let chars: Vec<(usize, char)> = title.char_indices().collect();
    let mut out = String::with_capacity(title.len());
    let mut cursor = 0usize;
    let mut i = 0usize;
    while i + 4 <= chars.len() {
        let window: String = chars[i..i + 4].iter().map(|(_, c)| *c).collect();
        if window.eq_ignore_ascii_case("[ui]") {
            out.push_str(&title[cursor..chars[i].0]);
            out.push(' ');
            cursor = chars.get(i + 4).map(|(byte, _)| *byte).unwrap_or(title.len());
            i += 4;
        } else {
            i += 1;
        }
    }
    out.push_str(&title[cursor..]);
    out
}

/// Splits a title into `(clean title, its domain tag or None)`.
///
/// A leading tag from the project's own vocabulary is the author's own claim, so
/// it is taken exactly as written. With no leading tag, the UI vocabulary decides;
/// a `[UI]` written mid-sentence still counts, as it always has.
fn split_tag(patterns: &Patterns, vocabulary: &Vocabulary, title: &str) -> (String, Option<String>) {
    let (clean, tag) = split_leading_tag(patterns, vocabulary, title);
    if tag.is_some() {
        return (clean, tag);
    }
    if let Some((start, end)) = find_ui_token(title) {
        let mut stripped = String::with_capacity(title.len());
        stripped.push_str(&title[..start]);
        stripped.push_str(&title[end..]);
        return (stripped.trim().to_string(), Some(vocabulary.ui_tag.clone()));
    }
    let trimmed = title.trim().to_string();
    if inferred_ui(patterns, &trimmed) {
        (trimmed, Some(vocabulary.ui_tag.clone()))
    } else {
        (trimmed, None)
    }
}

/// A task's deliverables declaration, e.g. "Files: a.py, b.py".
///
/// Both directions of the translation share this one pattern on purpose: an
/// asymmetric pair is exactly what caused every task's `files` to be silently
/// discarded whenever plan.json was rehydrated from the markdown plan.
fn deliverables_pattern() -> Regex {
    Regex::new(r"(?i)files?\s*(?:created|modified)?\s*:\s*([^,\n]+(?:,\s*[^,\n]+)*)")
        .expect("deliverables pattern")
}

/// File paths declared by a `Files: a.py, b.py` style detail line.
pub fn parse_deliverables(text: &str) -> Vec<String> {
    parse_deliverables_with(&deliverables_pattern(), text)
}

fn parse_deliverables_with(pattern: &Regex, text: &str) -> Vec<String> {
    let Some(captures) = pattern.captures(text) else {
        return Vec::new();
    };
    captures
        .get(1)
        .map(|group| {
            group
                .as_str()
                .split(',')
                .map(|part| part.trim_matches(|c| c == ' ' || c == '`' || c == '"' || c == '\'').to_string())
                .filter(|part| !part.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// Renders a files list as the detail line [`parse_deliverables`] reads back.
pub fn format_deliverables(files: &[String]) -> String {
    let rendered: Vec<String> = files.iter().map(|f| format!("`{}`", f)).collect();
    format!("Files: {}", rendered.join(", "))
}

/// Order-preserving de-duplication: `files` is a set of deliverables, so the same
/// path declared twice must not accumulate.
fn unique(files: Vec<String>) -> Vec<String> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    for file in files {
        if seen.insert(file.clone()) {
            out.push(file);
        }
    }
    out
}

fn status_from_mark(mark: &str) -> &'static str {
    if mark.to_lowercase() == "x" {
        return "completed";
    }
    match mark {
        "-" => "in_progress",
        "!" => "failed",
        _ => "pending",
    }
}

fn split_behavioral_logs(patterns: &Patterns, lines: &[String]) -> (Vec<String>, Vec<String>) {
    let mut logs: Vec<String> = Vec::new();
    let mut others: Vec<String> = Vec::new();
    for line in lines {
        match patterns.behavioral_log.captures(line) {
            Some(captures) => {
                let message = captures.get(1).map(|m| m.as_str().trim()).unwrap_or("");
                if !message.is_empty() {
                    logs.push(message.to_string());
                }
            }
            None => others.push(line.clone()),
        }
    }
    (logs, others)
}

/// Sorts detail bullets into a task's ledger, structured `files`, or `details`.
fn absorb_detail_lines(patterns: &Patterns, task: &mut Value, lines: &[String]) {
    let (logs, others) = split_behavioral_logs(patterns, lines);
    if !logs.is_empty() {
        if task.get("behavioral_log").and_then(Value::as_array).is_none() {
            task["behavioral_log"] = json!([]);
        }
        let existing = task["behavioral_log"].as_array_mut().expect("behavioral_log array");
        for message in logs {
            if !existing.iter().any(|entry| entry.as_str() == Some(message.as_str())) {
                existing.push(Value::String(message));
            }
        }
    }
    for line in others {
        let declared = parse_deliverables_with(&patterns.deliverables, &line);
        if !declared.is_empty() {
            let mut files: Vec<String> = as_array(task.get("files"))
                .iter()
                .filter_map(|f| f.as_str().map(|s| s.to_string()))
                .collect();
            files.extend(declared);
            task["files"] = json!(unique(files));
        } else {
            if task.get("details").and_then(Value::as_array).is_none() {
                task["details"] = json!([]);
            }
            task["details"]
                .as_array_mut()
                .expect("details array")
                .push(Value::String(line));
        }
    }
}

// ---------------------------------------------------------------------------
// The state summary and the sub-step tree
// ---------------------------------------------------------------------------

/// Captures the optional `## Global State Summary` block.
///
/// Returns `(summary, start, end)` where `start`/`end` are token indices, so the
/// task scan can walk past the block instead of turning its prose bullets into
/// milestones.
fn collect_state_summary(tokens: &[Token], patterns: &Patterns) -> (Option<Value>, Option<usize>, Option<usize>) {
    for (i, token) in tokens.iter().enumerate() {
        if token.kind != Kind::HeadingOpen || (token.tag != "h2" && token.tag != "h3") {
            continue;
        }
        let Some(next) = tokens.get(i + 1) else { continue };
        if next.kind != Kind::Inline {
            continue;
        }
        let heading_text = next.text.trim();
        if !patterns.state_summary.is_match(heading_text) {
            continue;
        }
        let mut bullets: Vec<Value> = Vec::new();
        let mut end = tokens.len();
        for (j, candidate) in tokens.iter().enumerate().skip(i + 2) {
            if candidate.kind == Kind::HeadingOpen {
                end = j;
                break;
            }
            if candidate.kind == Kind::Inline {
                let text = candidate.text.trim();
                if !text.is_empty() {
                    bullets.push(Value::String(text.to_string()));
                }
            }
        }
        let summary = json!({"title": heading_text, "bullets": bullets});
        return (Some(summary), Some(i), Some(end));
    }
    (None, None, None)
}

/// One list item during the sub-step pre-pass.
struct Node {
    index: usize,
    text: Option<String>,
    details: Vec<String>,
    children: Vec<usize>,
}

struct SubSteps {
    /// Raw parent text -> queue of its sub-step lists.
    by_task: HashMap<String, Vec<Vec<Value>>>,
    nested_indices: HashSet<usize>,
    nested_texts: HashSet<String>,
}

/// Finds the checkbox items indented under a top-level checkbox task.
///
/// Runs as a separate pass so the milestone scan keeps behaving exactly as it did:
/// each parent used to absorb its *first* nested checkbox into `details` and
/// harden every remaining one into a sibling milestone.
fn collect_sub_steps(tokens: &[Token], patterns: &Patterns, vocabulary: &Vocabulary) -> SubSteps {
    let mut arena: Vec<Node> = Vec::new();
    let mut stack: Vec<usize> = Vec::new();
    let mut top_level: Vec<usize> = Vec::new();

    for (index, token) in tokens.iter().enumerate() {
        match token.kind {
            Kind::ListItemOpen => {
                arena.push(Node {
                    index,
                    text: None,
                    details: Vec::new(),
                    children: Vec::new(),
                });
                stack.push(arena.len() - 1);
            }
            Kind::Inline if !stack.is_empty() => {
                let frame = *stack.last().expect("frame");
                let text = token.text.trim().to_string();
                if arena[frame].text.is_none() {
                    arena[frame].text = Some(text);
                } else {
                    arena[frame].details.push(text);
                }
            }
            Kind::ListItemClose if !stack.is_empty() => {
                let frame = stack.pop().expect("frame");
                match stack.last() {
                    Some(parent) => arena[*parent].children.push(frame),
                    None => top_level.push(frame),
                }
            }
            _ => {}
        }
    }

    let mut nested_indices: HashSet<usize> = HashSet::new();
    let mut nested_texts: HashSet<String> = HashSet::new();
    let mut by_task: HashMap<String, Vec<Vec<Value>>> = HashMap::new();

    for frame in top_level {
        let is_task = arena[frame]
            .text
            .as_deref()
            .map(|text| patterns.checkbox.is_match(text))
            .unwrap_or(false);
        if !is_task {
            continue;
        }
        let mut collected: Vec<usize> = Vec::new();
        for child in arena[frame].children.clone() {
            // A fresh state per direct child: the task's own nested bullets are
            // siblings of its sub-steps at the same indentation, so they must not
            // inherit the sub-step a previous sibling opened. Only a frame nested
            // *inside* a sub-step attaches to it.
            let mut current: Option<usize> = None;
            collect_sub_step(
                &mut arena,
                child,
                true,
                &mut current,
                &mut collected,
                &mut nested_indices,
                &mut nested_texts,
                patterns,
            );
        }
        if collected.is_empty() {
            continue;
        }
        let mut sub_steps: Vec<Value> = Vec::new();
        for item in collected {
            let text = arena[item].text.clone().unwrap_or_default();
            let Some(captures) = patterns.checkbox.captures(&text) else { continue };
            let mark = captures.get(1).map(|m| m.as_str()).unwrap_or(" ");
            let raw_title = captures.get(2).map(|m| m.as_str()).unwrap_or("");
            let (clean_title, tag) = split_tag(patterns, vocabulary, raw_title);
            let mut sub_step = json!({
                // Filled in by the caller, which is where the parent's id is known.
                "id": Value::Null,
                "title": clean_title,
                "status": status_from_mark(mark),
                "tag": tag,
                "details": [],
                "files": [],
                "behavioral_log": [],
            });
            let details = arena[item].details.clone();
            absorb_detail_lines(patterns, &mut sub_step, &details);
            sub_steps.push(sub_step);
        }
        // A queue, not a single list: two tasks may legitimately share a title.
        by_task
            .entry(arena[frame].text.clone().unwrap_or_default())
            .or_default()
            .push(sub_steps);
    }

    SubSteps { by_task, nested_indices, nested_texts }
}

#[allow(clippy::too_many_arguments)]
fn collect_sub_step(
    arena: &mut Vec<Node>,
    frame: usize,
    inside_step: bool,
    current: &mut Option<usize>,
    out: &mut Vec<usize>,
    nested_indices: &mut HashSet<usize>,
    nested_texts: &mut HashSet<String>,
    patterns: &Patterns,
) {
    let text = arena[frame].text.clone();
    let is_checkbox = text
        .as_deref()
        .map(|text| patterns.checkbox.is_match(text))
        .unwrap_or(false);

    if is_checkbox && inside_step {
        out.push(frame);
        nested_indices.insert(arena[frame].index);
        if let Some(text) = text.as_deref() {
            if !text.is_empty() {
                nested_texts.insert(text.to_string());
            }
        }
        for detail in &arena[frame].details {
            nested_texts.insert(detail.clone());
        }
        // A nested checkbox opens a new sub-step; everything indented under it
        // belongs to it rather than to the milestone above it.
        *current = Some(frame);
    } else if inside_step {
        if let Some(text) = text.as_deref() {
            if !text.is_empty() {
                if let Some(current_frame) = *current {
                    // A non-checkbox item indented under a sub-step is that
                    // sub-step's detail, recorded on the sub-step *and* claimed
                    // from the parent's detail scan.
                    arena[current_frame].details.push(text.to_string());
                    nested_texts.insert(text.to_string());
                }
            }
        }
    }

    for child in arena[frame].children.clone() {
        collect_sub_step(
            arena,
            child,
            inside_step || is_checkbox,
            current,
            out,
            nested_indices,
            nested_texts,
            patterns,
        );
    }
}

// ---------------------------------------------------------------------------
// The translation itself
// ---------------------------------------------------------------------------

/// Parses markdown plan text into a strict plan dictionary.
///
/// If `relaxed` is set, standard bulleted and numbered list items are extracted as
/// pending tasks.
pub fn parse_markdown_to_plan_dict(
    content: &str,
    filename: &str,
    relaxed: bool,
    vocabulary: &Vocabulary,
) -> Value {
    let (value, steps_empty) = parse_once(content, filename, relaxed, vocabulary);
    if steps_empty && !relaxed && !content.trim().is_empty() {
        // Fall back to relaxed parsing to capture numbered lists or un-checkboxed
        // task lists.
        return parse_once(content, filename, true, vocabulary).0;
    }
    value
}

/// A section while the document is being walked.
///
/// Tasks are held once, in `all_steps`, and a section remembers *which* of them
/// belong to it. The Python original appended the same task object to both the
/// section and the flat list, so a later detail absorbed through the flat view was
/// visible in the nested one; cloning the task into each would quietly drop those
/// edits from the section, which is what the compiler reads.
struct SectionBuilder {
    id: String,
    title: String,
    task_indices: Vec<usize>,
}

impl SectionBuilder {
    fn new(id: String, title: String) -> Self {
        SectionBuilder { id, title, task_indices: Vec::new() }
    }

    fn push_task(&mut self, all_steps: &mut Vec<Value>, task: Value) {
        all_steps.push(task);
        self.task_indices.push(all_steps.len() - 1);
    }

    fn materialize(&self, all_steps: &[Value]) -> Value {
        let tasks: Vec<Value> = self.task_indices.iter().map(|index| all_steps[*index].clone()).collect();
        json!({"id": self.id, "title": self.title, "tasks": tasks})
    }
}

fn parse_once(content: &str, filename: &str, relaxed: bool, vocabulary: &Vocabulary) -> (Value, bool) {
    let patterns = Patterns::new(vocabulary);
    let tokens = tokenize(content);

    let (state_summary, summary_start, summary_end) = collect_state_summary(&tokens, &patterns);
    let mut sub_steps = collect_sub_steps(&tokens, &patterns, vocabulary);

    let mut title = "Project Plan".to_string();
    let mut sections: Vec<SectionBuilder> = Vec::new();
    let mut current_section = SectionBuilder::new("sec-1".to_string(), "General".to_string());
    let mut all_steps: Vec<Value> = Vec::new();
    let mut task_counter = 0usize;
    let mut section_counter = 0usize;

    let mut i = 0usize;
    while i < tokens.len() {
        let token = &tokens[i];

        // 0. The Global State Summary is standing context, not a milestone list.
        if let (Some(start), Some(end)) = (summary_start, summary_end) {
            if start < i && i < end {
                i += 1;
                continue;
            }
        }

        // 1. Headers (h1 -> Plan Title, h2/h3 -> Section or Logged Task)
        if token.kind == Kind::HeadingOpen {
            let tag = token.tag;
            if let Some(next) = tokens.get(i + 1) {
                if next.kind == Kind::Inline {
                    let heading_text = next.text.trim().to_string();
                    if tag == "h1" {
                        title = match heading_text.split_once(':') {
                            Some((_, rest)) => rest.trim().to_string(),
                            None => heading_text,
                        };
                    } else if tag == "h2" || tag == "h3" {
                        if summary_start == Some(i) {
                            // Not a section: it holds no tasks and is already captured.
                            i += 2;
                            continue;
                        }
                        let task_header = fixed_patterns()
                            .task_header
                            .captures(&heading_text)
                            .map(|captures| captures.get(1).map(|m| m.as_str().trim().to_string()))
                            .flatten();
                        // Only a heading that opens with a *real* tag is a logged task.
                        let is_logged_task = task_header
                            .as_deref()
                            .map(|name| is_tag(vocabulary, name))
                            .unwrap_or(false);
                        if is_logged_task {
                            task_counter += 1;
                            let header = task_header.unwrap_or_default();
                            let (clean_title, tag) = split_tag(&patterns, vocabulary, &header);
                            let task_obj = json!({
                                "id": format!("task-{}", task_counter),
                                "section": current_section.title,
                                "title": clean_title,
                                "status": "completed",
                                "tag": tag,
                                "details": [],
                                "files": [],
                                "behavioral_log": [],
                                "sub_steps": [],
                            });
                            current_section.push_task(&mut all_steps, task_obj);
                        } else {
                            if !current_section.task_indices.is_empty() || section_counter > 0 {
                                sections.push(current_section);
                            }
                            section_counter += 1;
                            current_section = SectionBuilder::new(
                                format!("sec-{}", section_counter),
                                heading_text,
                            );
                        }
                    }
                }
            }
            i += 2;
            continue;
        }

        // 2. List items (tasks with checkboxes or sub-bullets)
        if token.kind == Kind::ListItemOpen {
            let mut j = i + 1;
            let mut item_text: Option<String> = None;
            let mut item_details: Vec<String> = Vec::new();

            while j < tokens.len() && tokens[j].kind != Kind::ListItemClose {
                if tokens[j].kind == Kind::Inline {
                    let text = tokens[j].text.trim().to_string();
                    if item_text.is_none() {
                        item_text = Some(text);
                    } else {
                        item_details.push(text);
                    }
                }
                j += 1;
            }

            if sub_steps.nested_indices.contains(&i) {
                // Part of the card of an earlier task, not a milestone of its own.
                i = j + 1;
                continue;
            }

            if let Some(text) = item_text.clone() {
                let checkbox = patterns.checkbox.captures(&text);
                match checkbox {
                    Some(captures) => {
                        let mark = captures.get(1).map(|m| m.as_str()).unwrap_or(" ");
                        let raw_task_title = captures.get(2).map(|m| m.as_str()).unwrap_or("");
                        task_counter += 1;
                        let (clean_task_title, tag) = split_tag(&patterns, vocabulary, raw_task_title);

                        let mut task_obj = json!({
                            "id": format!("task-{}", task_counter),
                            "section": current_section.title,
                            "title": clean_task_title,
                            "status": status_from_mark(mark),
                            "tag": tag,
                            "details": [],
                            "files": [],
                            "behavioral_log": [],
                            "sub_steps": [],
                        });
                        if let Some(queues) = sub_steps.by_task.get_mut(&text) {
                            if !queues.is_empty() {
                                // Keyed by the item's own text so the sub-steps land on
                                // the task they were indented under.
                                let mut steps = queues.remove(0);
                                for (position, step) in steps.iter_mut().enumerate() {
                                    step["id"] = json!(format!(
                                        "task-{}-sub-{}",
                                        task_counter,
                                        position + 1
                                    ));
                                }
                                task_obj["sub_steps"] = json!(steps);
                            }
                        }
                        // Sub-steps own their own lines now, so they must not also
                        // arrive as detail bullets of the parent.
                        let filtered: Vec<String> = item_details
                            .iter()
                            .filter(|detail| !sub_steps.nested_texts.contains(*detail))
                            .cloned()
                            .collect();
                        absorb_detail_lines(&patterns, &mut task_obj, &filtered);
                        current_section.push_task(&mut all_steps, task_obj);
                    }
                    None => {
                        if relaxed {
                            let clean_item = fixed_patterns()
                                .bullet_prefix
                                .replace(&text, "")
                                .trim()
                                .to_string();
                            if !clean_item.is_empty()
                                && clean_item.chars().count() > 2
                                && !clean_item.starts_with("http")
                            {
                                task_counter += 1;
                                let (clean_task_title, tag) =
                                    split_tag(&patterns, vocabulary, &clean_item);
                                let (logs, raw_details) =
                                    split_behavioral_logs(&patterns, &item_details);
                                let task_obj = json!({
                                    "id": format!("task-{}", task_counter),
                                    "section": current_section.title,
                                    "title": clean_task_title,
                                    "status": "pending",
                                    "tag": tag,
                                    "details": raw_details,
                                    "files": [],
                                    "behavioral_log": logs,
                                    "sub_steps": [],
                                });
                                current_section.push_task(&mut all_steps, task_obj);
                            }
                        } else if let Some(last) = all_steps.last_mut() {
                            // Nested bullets past the first arrive here instead of the
                            // checkbox branch above (the item scan stops at the first
                            // list_item_close), so they must be sorted the same way.
                            let mut lines = vec![text.clone()];
                            lines.extend(item_details.clone());
                            absorb_detail_lines(&patterns, last, &lines);
                        }
                    }
                }
            }
            i = j;
        }

        i += 1;
    }

    let current_value = current_section.materialize(&all_steps);
    let mut section_values: Vec<Value> = sections
        .iter()
        .map(|section| section.materialize(&all_steps))
        .collect();
    if !section_values.iter().any(|section| section == &current_value)
        && !current_section.task_indices.is_empty()
    {
        section_values.push(current_value.clone());
    } else if section_values.is_empty() {
        section_values.push(current_value);
    }

    let total = all_steps.len();
    let completed = count_status(&all_steps, "completed");
    let in_progress = count_status(&all_steps, "in_progress");
    let failed = count_status(&all_steps, "failed");
    let pending = total - completed - in_progress - failed;
    // `round` is Python's, which breaks a tie to the even integer; `round` in Rust
    // breaks it away from zero, so 1 of 8 tasks would report 13% instead of 12%.
    let pct = if total > 0 {
        (completed as f64 / total as f64 * 100.0).round_ties_even() as i64
    } else {
        0
    };

    let value = json!({
        "version": "1.0",
        "plan_file": filename,
        "title": title,
        "state_summary": state_summary,
        "updated_at": crate::state::now_seconds(),
        "sections": section_values,
        "steps": all_steps,
        "metrics": {
            "total_tasks": total,
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "failed_tasks": failed,
            "pending_tasks": pending,
            "progress_percent": pct,
        }
    });
    (value, all_steps.is_empty())
}

fn count_status(steps: &[Value], status: &str) -> usize {
    steps
        .iter()
        .filter(|step| step.get("status").and_then(Value::as_str) == Some(status))
        .count()
}

/// Compiles a strict plan dictionary back into standardized markdown.
pub fn compile_plan_json_to_markdown(data: &Value, vocabulary: &Vocabulary) -> String {
    let title = as_str_or(data.get("title"), "Project Plan");
    let mut lines: Vec<String> = vec![format!("# Project Plan: {}", title), String::new()];

    // The Global State Summary is standing context rather than a milestone, so it is
    // re-emitted directly under the title; without this the block would be erased by
    // the first save and every later context slice would lose the plan's core facts.
    let summary = data.get("state_summary").unwrap_or(&Value::Null);
    let bullets = as_array(summary.get("bullets"));
    if !bullets.is_empty() {
        let summary_title = non_empty_str(summary.get("title")).unwrap_or(STATE_SUMMARY_TITLE);
        lines.push(format!("## {}", summary_title));
        for bullet in bullets {
            lines.push(format!("- {}", bullet.as_str().unwrap_or("")));
        }
        lines.push(String::new());
        lines.push("---".to_string());
        lines.push(String::new());
    }

    for section in as_array(data.get("sections")) {
        let section_title = as_str_or(section.get("title"), "General");
        lines.push(format!("## {}", section_title));

        for task in as_array(section.get("tasks")) {
            let status = as_str_or(task.get("status"), "pending");
            let mark = MARK_FOR_STATUS
                .iter()
                .find(|(name, _)| *name == status)
                .map(|(_, mark)| *mark)
                .unwrap_or(" ");
            // The tag the plan carries is written back as itself; a task whose domain
            // was inferred still carries it in `tag`, so there is no second field.
            let prefix = match non_empty_str(task.get("tag")) {
                Some(tag) => tag_prefix(vocabulary, tag),
                None => String::new(),
            };
            let task_title = as_str_or(task.get("title"), "");
            let files: Vec<String> = as_array(task.get("files"))
                .iter()
                .filter_map(|f| f.as_str().map(|s| s.to_string()))
                .filter(|f| !f.is_empty())
                .collect();
            lines.push(format!("- [{}] {}{}", mark, prefix, task_title));
            // The inline ledger sits directly beneath the checkbox it describes.
            for entry in as_array(task.get("behavioral_log")) {
                lines.push(format!("  - {} {}", BEHAVIORAL_LOG_PREFIX, entry.as_str().unwrap_or("")));
            }
            // Sub-steps are re-indented under their parent so the parser folds them
            // back into `sub_steps` instead of rebuilding them as extra milestones.
            for sub_step in as_array(task.get("sub_steps")) {
                let sub_status = as_str_or(sub_step.get("status"), "pending");
                let sub_mark = MARK_FOR_STATUS
                    .iter()
                    .find(|(name, _)| *name == sub_status)
                    .map(|(_, mark)| *mark)
                    .unwrap_or(" ");
                let sub_prefix = match non_empty_str(sub_step.get("tag")) {
                    Some(tag) => tag_prefix(vocabulary, tag),
                    None => String::new(),
                };
                lines.push(format!(
                    "  - [{}] {}{}",
                    sub_mark,
                    sub_prefix,
                    as_str_or(sub_step.get("title"), "")
                ));
                for entry in as_array(sub_step.get("behavioral_log")) {
                    lines.push(format!(
                        "    - {} {}",
                        BEHAVIORAL_LOG_PREFIX,
                        entry.as_str().unwrap_or("")
                    ));
                }
                for detail in as_array(sub_step.get("details")) {
                    lines.push(format!("    - {}", detail.as_str().unwrap_or("")));
                }
                let sub_files: Vec<String> = as_array(sub_step.get("files"))
                    .iter()
                    .filter_map(|f| f.as_str().map(|s| s.to_string()))
                    .filter(|f| !f.is_empty())
                    .collect();
                if !sub_files.is_empty() {
                    lines.push(format!("    - {}", format_deliverables(&sub_files)));
                }
            }
            for detail in as_array(task.get("details")) {
                lines.push(format!("  - {}", detail.as_str().unwrap_or("")));
            }
            if !files.is_empty() {
                // Without this line the markdown carries no record of a task's
                // deliverables, so load_plan_state() lost every `files` entry
                // whenever it rehydrated plan.json from the markdown plan.
                lines.push(format!("  - {}", format_deliverables(&files)));
            }
        }
        lines.push(String::new());
    }

    format!("{}\n", lines.join("\n").trim())
}

/// Verdict on whether a document carries the plan AST the parser can read.
pub fn check_plan_structure(content: &str) -> Value {
    let fixed = fixed_patterns();

    let text = content;
    let lines = split_lines(text);
    let sections = lines.iter().filter(|line| fixed.section_line.is_match(line)).count();
    let milestones = lines
        .iter()
        .filter(|line| fixed.checkbox.is_match(line.trim()))
        .count();
    let bullets = lines.iter().filter(|line| fixed.any_bullet.is_match(line)).count();
    let has_title = lines.iter().any(|line| fixed.title_line.is_match(line));

    let mut issues: Vec<String> = Vec::new();
    if text.trim().is_empty() {
        issues.push("The document is empty.".to_string());
    } else {
        if !has_title {
            issues.push("Add a `# Title` heading.".to_string());
        }
        if sections == 0 {
            issues.push("Add at least one `## Section` heading.".to_string());
        }
        if milestones == 0 {
            issues.push("Mark each milestone as a `- [ ]` checkbox.".to_string());
        }
    }

    let structured = !text.trim().is_empty() && has_title && sections > 0 && milestones > 0;
    let summary = if structured {
        format!("Structured: {} section(s), {} milestone(s).", sections, milestones)
    } else if text.trim().is_empty() {
        "Unstructured: the document is empty.".to_string()
    } else {
        format!("Unstructured: {}", issues.join(" "))
    };

    json!({
        "structured": structured,
        "issues": issues,
        "summary": summary,
        "counts": {
            "sections": sections,
            "milestones": milestones,
            "bullets": bullets,
            "has_title": has_title,
        }
    })
}

/// Every leading tag the markdown actually carries, as `{clean title: tag}`.
pub fn explicit_tags(content: &str, vocabulary: &Vocabulary) -> BTreeMap<String, String> {
    let patterns = Patterns::new(vocabulary);
    let checkbox_re = &patterns.checkbox;
    let mut found: BTreeMap<String, String> = BTreeMap::new();
    for line in split_lines(content) {
        let Some(captures) = checkbox_re.captures(line.trim()) else { continue };
        let raw = captures.get(2).map(|m| m.as_str()).unwrap_or("");
        let (clean, tag) = split_leading_tag(&patterns, vocabulary, raw);
        if tag.is_some() {
            found.insert(clean, tag.unwrap_or_default());
        } else if let Some((start, end)) = find_ui_token(raw) {
            // A [UI] written mid-sentence rather than as a prefix still counts.
            let mut stripped = String::with_capacity(raw.len());
            stripped.push_str(&raw[..start]);
            stripped.push_str(&raw[end..]);
            found.insert(stripped.trim().to_string(), vocabulary.ui_tag.clone());
        }
    }
    found
}

/// Titles carrying a *literal* `[UI]` tag.
pub fn explicit_ui_titles(content: &str, vocabulary: &Vocabulary) -> BTreeSet<String> {
    explicit_tags(content, vocabulary)
        .into_iter()
        .filter(|(_, tag)| *tag == vocabulary.ui_tag)
        .map(|(title, _)| title)
        .collect()
}

/// The plan dictionary's own `steps` list, parsed from markdown.
pub fn parse_plan_tree(content: &str, filename: &str, vocabulary: &Vocabulary) -> Value {
    let parsed = parse_markdown_to_plan_dict(content, filename, false, vocabulary);
    parsed.get("steps").cloned().unwrap_or_else(|| json!([]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vocabulary() -> Vocabulary {
        let mut canonical = HashMap::new();
        for tag in ["FE", "BE", "API", "TEST", "DOCS"] {
            canonical.insert(tag.to_string(), tag.to_string());
        }
        canonical.insert("UI".to_string(), "FE".to_string());
        canonical.insert("BIZ".to_string(), "BE".to_string());
        Vocabulary {
            canonical,
            ui_tag: "FE".to_string(),
            ui_keyword_pattern: r"\b(?:ui|frontend|interface|view)s?\b".to_string(),
        }
    }

    #[test]
    fn parses_tasks_sections_and_status_marks() {
        let content = "\
# Project Plan: Demo

## 1. Build

- [x] Ship the parser
- [-] Wire the bridge
- [ ] Write the docs
- [!] Fix the crash
";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        assert_eq!(plan["title"], "Demo");
        assert_eq!(plan["sections"][0]["title"], "1. Build");
        let steps = plan["steps"].as_array().expect("steps");
        assert_eq!(steps.len(), 4);
        assert_eq!(steps[0]["id"], "task-1");
        assert_eq!(steps[0]["status"], "completed");
        assert_eq!(steps[1]["status"], "in_progress");
        assert_eq!(steps[2]["status"], "pending");
        assert_eq!(steps[3]["status"], "failed");
        assert_eq!(plan["metrics"]["progress_percent"], 25);
    }

    #[test]
    fn nested_checkboxes_become_sub_steps_not_milestones() {
        let content = "\
## 1. Build

- [ ] Parent milestone
  - [ ] Sub one
  - [ ] Sub two
  - a plain detail
";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        let steps = plan["steps"].as_array().expect("steps");
        assert_eq!(steps.len(), 1);
        let sub_steps = steps[0]["sub_steps"].as_array().expect("sub_steps");
        assert_eq!(sub_steps.len(), 2);
        assert_eq!(sub_steps[0]["id"], "task-1-sub-1");
        assert_eq!(sub_steps[0]["title"], "Sub one");
        let details: Vec<&str> = steps[0]["details"]
            .as_array()
            .expect("details")
            .iter()
            .map(|d| d.as_str().unwrap_or(""))
            .collect();
        assert_eq!(details, vec!["a plain detail"]);
    }

    #[test]
    fn behavioral_log_and_deliverables_are_structured() {
        let content = "\
## 1. Build

- [x] Ship it
  - \u{1F7E2} Behavioral Log: wrote the module
  - Files: `a.py`, `b.py`
";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        let step = &plan["steps"][0];
        assert_eq!(step["behavioral_log"][0], "wrote the module");
        let files: Vec<&str> = step["files"]
            .as_array()
            .expect("files")
            .iter()
            .map(|f| f.as_str().unwrap_or(""))
            .collect();
        assert_eq!(files, vec!["a.py", "b.py"]);
        assert_eq!(step["details"].as_array().expect("details").len(), 0);
    }

    #[test]
    fn ui_tag_is_inferred_from_wording() {
        let content = "## 1. Build\n\n- [ ] Build the dashboard view\n- [ ] Build the API\n";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        assert_eq!(plan["steps"][0]["tag"], "FE");
        assert_eq!(plan["steps"][1]["tag"], Value::Null);
    }

    #[test]
    fn explicit_tag_wins_and_legacy_alias_canonicalises() {
        let content = "## 1. Build\n\n- [ ] [UI] Wire the panel\n- [ ] [BIZ] Model the domain\n";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        assert_eq!(plan["steps"][0]["title"], "Wire the panel");
        assert_eq!(plan["steps"][0]["tag"], "FE");
        assert_eq!(plan["steps"][1]["title"], "Model the domain");
        assert_eq!(plan["steps"][1]["tag"], "BE");
    }

    #[test]
    fn state_summary_is_captured_and_not_a_section() {
        let content = "\
# Project Plan: Demo

## \u{1F30D} Global State Summary

- **Fact:** something settled.

---

## 1. Build

- [ ] A milestone
";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        assert_eq!(plan["state_summary"]["bullets"][0], "**Fact:** something settled.");
        assert_eq!(plan["sections"].as_array().expect("sections").len(), 1);
        assert_eq!(plan["steps"].as_array().expect("steps").len(), 1);
    }

    #[test]
    fn round_trip_preserves_every_field() {
        let content = "\
# Project Plan: Demo

## \u{1F30D} Global State Summary

- Standing fact.

---

## 1. Build

- [x] [BE] Ship it
  - \u{1F7E2} Behavioral Log: wrote the module
  - Files: `a.py`, `b.py`
  - [ ] Sub one
    - \u{1F7E2} Behavioral Log: sub work
  - a detail note
";
        let first = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        let compiled = compile_plan_json_to_markdown(&first, &vocabulary());
        let second = parse_markdown_to_plan_dict(&compiled, "PLAN.md", false, &vocabulary());
        assert_eq!(first["steps"], second["steps"]);
        assert_eq!(first["state_summary"], second["state_summary"]);
        assert_eq!(first["title"], second["title"]);
    }

    #[test]
    fn relaxed_parsing_picks_up_plain_bullets() {
        let content = "## 1. Build\n\n- A plain item worth doing\n- Another one\n";
        let plan = parse_markdown_to_plan_dict(content, "PLAN.md", false, &vocabulary());
        assert_eq!(plan["steps"].as_array().expect("steps").len(), 2);
        assert_eq!(plan["steps"][0]["status"], "pending");
    }

    #[test]
    fn structure_check_reports_the_three_requirements() {
        let verdict = check_plan_structure("# Title\n\n## Section\n\n- [ ] Milestone\n");
        assert_eq!(verdict["structured"], true);
        let verdict = check_plan_structure("# Title\n\nprose only\n");
        assert_eq!(verdict["structured"], false);
        assert_eq!(verdict["counts"]["milestones"], 0);
    }

    #[test]
    fn deliverables_line_is_read_back_without_backticks() {
        assert_eq!(
            parse_deliverables("Files: `a.py`, b.py"),
            vec!["a.py".to_string(), "b.py".to_string()]
        );
        assert_eq!(parse_deliverables("no declaration here"), Vec::<String>::new());
    }
}
