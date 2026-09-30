//! Token-budgeted truncation.
//!
//! One job, and it exists to keep a model's context window honest: middle truncation, so a
//! large file read or a noisy shell command cannot blow the window. The head and the tail
//! are kept and the middle is replaced by a marker naming what was dropped, because the
//! first lines say what a file is and the last lines say how it ended.
//!
//! **The Line-Anchored Context Slicer used to live here and has been deleted.** Context for
//! a task is now assembled in Python (`orchestration/workflow/context.py`) from the task's
//! own structural slice plus the exact AST nodes retrieved from the workspace's vector
//! index -- so no code path slices a document by line number any more, and there is no FFI
//! surface left that could reintroduce it.
//!
//! Ported from `tools/file_ops.py`, `tools/shell_tools.py` and `tools/test_runner.py`. Every
//! function here is pure -- text in, a string out -- so the arithmetic that decides what a
//! model sees is in one auditable place.

/// The first `count` code points of `text`.
///
/// Slicing is by code point, not by byte, because the length a caller compares
/// against a character budget is Python's `len()` -- a distinction that only shows
/// up on non-ASCII text, which is exactly where an off-by-a-few error is invisible.
pub fn take_chars(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// The last `count` code points of `text`.
pub fn take_last_chars(text: &str, count: usize) -> String {
    let total = text.chars().count();
    if count >= total {
        return text.to_string();
    }
    text.chars().skip(total - count).collect()
}

/// A workspace file read, middle-truncated when it would overflow the budget.
///
/// Plan files and machine state are exempt: they are read whole or not at all,
/// because a truncated plan is a plan an agent will act on incorrectly.
pub fn truncate_for_read_file(content: &str, filename: &str, max_chars: usize) -> String {
    let count = content.chars().count();
    let lowered = filename.to_lowercase();
    let exempt = lowered.ends_with(".md") || lowered.ends_with(".json");
    if count <= max_chars || exempt {
        return content.to_string();
    }
    let half = max_chars / 2;
    let head = take_chars(content, half);
    let tail = take_last_chars(content, half);
    format!(
        "{}\n\n... [Omitted {} characters from {} to optimize context window] ...\n\n{}",
        head,
        count - max_chars,
        filename,
        tail
    )
}

/// A command's output, middle-truncated to keep its head and its exit trace.
pub fn truncate_shell_output(output: &str, max_chars: usize) -> String {
    let count = output.chars().count();
    if count <= max_chars {
        return output.to_string();
    }
    let half = max_chars / 2;
    let head = take_chars(output, half);
    let tail = take_last_chars(output, half);
    format!(
        "{}\n\n... [Omitted {} characters to preserve context window & optimize tokens] ...\n\n{}",
        head,
        count - max_chars,
        tail
    )
}

/// A test runner's output, middle-truncated to keep its head and its summary line.
///
/// The verdict the result view shows is read from that summary, so the tail matters
/// as much as the head; the marker is terser than the other two because this text is
/// already a report about a report.
pub fn truncate_test_output(output: &str, max_chars: usize) -> String {
    let count = output.chars().count();
    if count <= max_chars {
        return output.to_string();
    }
    let half = max_chars / 2;
    let head = take_chars(output, half);
    let tail = take_last_chars(output, half);
    format!("{}\n... [truncated] ...\n{}", head, tail)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_short_read_is_returned_untouched() {
        assert_eq!(truncate_for_read_file("short", "a.py", 6000), "short");
    }

    #[test]
    fn a_plan_file_is_never_truncated() {
        let content = "x".repeat(7000);
        assert_eq!(truncate_for_read_file(&content, "PLAN.md", 6000), content);
        assert_eq!(truncate_for_read_file(&content, "plan.json", 6000), content);
    }

    #[test]
    fn a_large_read_keeps_its_head_and_tail_and_names_the_loss() {
        let content: String = (0..7000).map(|i| char::from(b'a' + (i % 26) as u8)).collect();
        let view = truncate_for_read_file(&content, "big.py", 6000);
        assert!(view.starts_with(&content[..3000]));
        assert!(view.ends_with(&content[4000..]));
        assert!(view.contains("... [Omitted 1000 characters from big.py to optimize context window] ..."));
    }

    #[test]
    fn shell_output_is_truncated_with_its_own_marker() {
        let output = "y".repeat(2500);
        let view = truncate_shell_output(&output, 2400);
        assert!(view.contains("... [Omitted 100 characters to preserve context window & optimize tokens] ..."));
        assert_eq!(view.chars().count(), 2400 + "... [Omitted 100 characters to preserve context window & optimize tokens] ...".chars().count() + 4);
    }

    #[test]
    fn truncation_counts_code_points_not_bytes() {
        // 4000 three-byte characters: a byte-based slice would cut mid-character.
        let content = "\u{4e16}".repeat(4000);
        let view = truncate_for_read_file(&content, "wide.py", 100);
        assert!(view.starts_with(&"\u{4e16}".repeat(50)));
        assert!(view.contains("Omitted 3900 characters"));
    }

    #[test]
    fn test_output_is_truncated_with_its_summary_line() {
        let output = "z".repeat(3000);
        let view = truncate_test_output(&output, 2400);
        assert!(view.starts_with(&"z".repeat(1200)));
        assert!(view.ends_with(&"z".repeat(1200)));
        assert!(view.contains("\n... [truncated] ...\n"));
        assert_eq!(truncate_test_output("short", 2400), "short");
    }
}
