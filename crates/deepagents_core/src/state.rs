//! Plan-state helpers for the compiled core.
//!
//! **The dual-sync machine is gone.** Plan state lives in a SQLite DAG on the Python side
//! (`tools.plan_state` + `storage.db`), and the markdown plan is a projection rendered
//! from it. Nothing here reads markdown to determine state: the old
//! `load_plan_state` / `write_plan_pair` pair, `hydrate_from_markdown` and the mtime
//! comparison have all been deleted, so there is no FFI surface left that could
//! resurrect the dual-sync.
//!
//! What remains is the pure, non-markdown work the Python layer still calls:
//! canonicalising a plan dictionary ([`prepare_plan_state`]), the in-memory task edits
//! ([`update_plan_task`], [`append_pending_task`]), the structure verdict, and the atomic
//! markdown projection write.
//!
//! * **One lock covers the plan directory.** Writes happen under an exclusive OS-level
//!   lock (`fs2`), so two writers cannot interleave.
//! * **Writes are atomic.** Content goes to a temporary file in the same directory,
//!   is flushed to disk, and is then renamed over the target. A reader therefore
//!   sees either the old file or the new one, never a half-written one, and a crash
//!   mid-write cannot leave a truncated plan behind.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use fs2::FileExt;
use serde_json::{json, Value};

use crate::parser::check_plan_structure;

/// The lock file that guards the plan directory.
///
/// Kept out of the plan directory on purpose. The plan directory is normally the
/// user's own repository -- the roadmap lives beside the code it describes -- and a
/// lock file there would show up in `git status`, in the workspace file listing and
/// in the codebase audit as an untracked file. The lock is an implementation detail
/// of the core, so it lives in the system temporary directory instead, named after
/// a hash of the plan directory it guards.
const LOCK_SUBDIR: &str = "deepagents_plan_locks";

/// The path of the lock file that guards `plan_dir`.
///
/// The hash makes the name deterministic across processes -- which is the whole
/// point of a lock -- while keeping it short enough for any path the plan directory
/// might have.
fn lock_path(plan_dir: &Path) -> PathBuf {
    let canonical = std::fs::canonicalize(plan_dir).unwrap_or_else(|_| plan_dir.to_path_buf());
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in canonical.to_string_lossy().as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    let mut path = std::env::temp_dir();
    path.push(LOCK_SUBDIR);
    path.push(format!("{:016x}.lock", hash));
    path
}

/// A write (or its markdown compile) failed.
///
/// Surfaced to Python as a message rather than swallowed: a caller must not be
/// able to report `success` for state the disk never received.
pub type WriteFailure = String;

/// Epoch seconds, as Python's `time.time()` reports them.
pub fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

/// Holds the plan directory's exclusive lock for as long as it is alive.
pub struct PlanLock {
    file: File,
}

impl PlanLock {
    pub fn acquire(plan_dir: &Path) -> Result<PlanLock, String> {
        std::fs::create_dir_all(plan_dir).map_err(|error| error.to_string())?;
        let path = lock_path(plan_dir);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|error| format!("could not open {}: {}", path.display(), error))?;
        file.lock_exclusive()
            .map_err(|error| format!("could not lock {}: {}", path.display(), error))?;
        Ok(PlanLock { file })
    }
}

impl Drop for PlanLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.file);
    }
}

/// Writes `contents` to `path` through a temporary file and a rename.
///
/// The rename is retried briefly on failure. On Windows a rename over a file that
/// another process holds open for reading fails with a sharing violation, and the
/// readers here are short-lived by nature -- a file explorer, an agent's `read_file`,
/// a test measuring the plan -- so a few milliseconds is the difference between an
/// intermittent failure and a write that never notices.
pub fn atomic_write(path: &Path, contents: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let mut temporary = path.as_os_str().to_os_string();
    temporary.push(format!(".tmp{}", std::process::id()));
    let temporary = PathBuf::from(temporary);

    let write_result = (|| -> std::io::Result<()> {
        let mut file = File::create(&temporary)?;
        file.write_all(contents.as_bytes())?;
        // Flushed before the rename so the rename can only ever publish bytes that
        // are actually on disk.
        file.sync_all()?;
        Ok(())
    })();
    if let Err(error) = write_result {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }

    // `fs::rename` replaces an existing file on Windows as well as on Unix, which is
    // what makes the swap atomic on both.
    let mut last_error = None;
    for attempt in 0..RENAME_ATTEMPTS {
        match std::fs::rename(&temporary, path) {
            Ok(()) => return Ok(()),
            Err(error) => {
                last_error = Some(error);
                if attempt + 1 < RENAME_ATTEMPTS {
                    std::thread::sleep(RENAME_RETRY_DELAY);
                }
            }
        }
    }
    let _ = std::fs::remove_file(&temporary);
    Err(last_error.unwrap_or_else(|| std::io::Error::other("rename failed")))
}

/// Ten attempts at 15ms bounds the delay at ~150ms while covering the read that a
/// sharing violation is almost always waiting on.
const RENAME_ATTEMPTS: usize = 10;
const RENAME_RETRY_DELAY: std::time::Duration = std::time::Duration::from_millis(15);

/// Recomputes a plan's derived state in place: the flattened `steps` view, the
/// progress metrics, `updated_at` and `plan_file`.
///
/// Separated from the write so the caller keeps ownership of *when* the store is
/// committed and of compiling the markdown projection. Nothing here touches the disk.
pub fn prepare_plan_state(plan: &mut Value, active_plan: &str) -> Result<(), WriteFailure> {
    if !plan.is_object() {
        return Err("the plan state is not an object".to_string());
    }

    // Flatten steps from sections to ensure consistency. A dict that carries tasks
    // only in the flat `steps` view would otherwise flatten to nothing -- wiping
    // `steps` and zeroing every metric. Rebuilding the nested view from the flat one
    // first keeps such a plan intact.
    let sections_empty = plan
        .get("sections")
        .and_then(Value::as_array)
        .map(|sections| sections.is_empty())
        .unwrap_or(true);
    if sections_empty {
        let flat: Vec<Value> = plan
            .get("steps")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if !flat.is_empty() {
            let mut grouped: Vec<(String, Value)> = Vec::new();
            for mut task in flat {
                let title = task
                    .get("section")
                    .and_then(Value::as_str)
                    .unwrap_or("General")
                    .to_string();
                let position = match grouped.iter().position(|(name, _)| *name == title) {
                    Some(position) => position,
                    None => {
                        grouped.push((
                            title.clone(),
                            json!({"id": format!("sec-{}", grouped.len() + 1), "title": title, "tasks": []}),
                        ));
                        grouped.len() - 1
                    }
                };
                if let Some(tasks) = grouped[position].1.get_mut("tasks").and_then(Value::as_array_mut) {
                    tasks.push(task.take());
                }
            }
            plan["sections"] = Value::Array(grouped.into_iter().map(|(_, section)| section).collect());
        }
    }

    let mut all_steps: Vec<Value> = Vec::new();
    if let Some(sections) = plan.get_mut("sections").and_then(Value::as_array_mut) {
        for section in sections.iter_mut() {
            let section_title = section
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("General")
                .to_string();
            if let Some(tasks) = section.get_mut("tasks").and_then(Value::as_array_mut) {
                for task in tasks.iter_mut() {
                    task["section"] = Value::String(section_title.clone());
                    all_steps.push(task.clone());
                }
            }
        }
    }

    let total = all_steps.len();
    let count = |status: &str| {
        all_steps
            .iter()
            .filter(|task| task.get("status").and_then(Value::as_str) == Some(status))
            .count()
    };
    let completed = count("completed");
    let in_progress = count("in_progress");
    let failed = count("failed");
    let pending = total - completed - in_progress - failed;
    // Python's `round`, which breaks a tie to the even integer.
    let percent = if total > 0 {
        (completed as f64 / total as f64 * 100.0).round_ties_even() as i64
    } else {
        0
    };

    plan["steps"] = Value::Array(all_steps);
    plan["metrics"] = json!({
        "total_tasks": total,
        "completed_tasks": completed,
        "in_progress_tasks": in_progress,
        "failed_tasks": failed,
        "pending_tasks": pending,
        "progress_percent": percent,
    });
    plan["updated_at"] = json!(now_seconds());
    plan["plan_file"] = json!(active_plan);
    Ok(())
}

/// The active plan's markdown projection as text, or `""` when it is not on disk.
pub fn read_plan_markdown(plan_md_path: &str) -> String {
    std::fs::read_to_string(plan_md_path).unwrap_or_default()
}

/// Writes the active plan's markdown projection into the plan directory, atomically.
pub fn write_plan_markdown(plan_md_path: &str, content: &str) -> Result<String, WriteFailure> {
    let path = Path::new(plan_md_path);
    let plan_dir = path.parent().unwrap_or(Path::new("."));
    let _guard = PlanLock::acquire(plan_dir)?;
    atomic_write(path, content).map_err(|error| error.to_string())?;
    Ok(plan_md_path.to_string())
}

/// Updates a specific task's status in the plan, in memory.
///
/// Returns whether the task was found. The write is the caller's to make, so a
/// single save path owns publishing the projection.
#[allow(clippy::too_many_arguments)]
pub fn update_plan_task(
    plan: &mut Value,
    task_id: &str,
    new_status: &str,
    detail_note: Option<&str>,
    files: &[String],
) -> bool {
    let mut found = false;
    if let Some(sections) = plan.get_mut("sections").and_then(Value::as_array_mut) {
        'outer: for section in sections.iter_mut() {
            if let Some(tasks) = section.get_mut("tasks").and_then(Value::as_array_mut) {
                for task in tasks.iter_mut() {
                    let id_matches = task.get("id").and_then(Value::as_str) == Some(task_id);
                    let title_matches = task.get("title").and_then(Value::as_str) == Some(task_id);
                    if !id_matches && !title_matches {
                        continue;
                    }
                    task["status"] = Value::String(new_status.to_string());
                    if let Some(note) = detail_note {
                        if task.get("details").and_then(Value::as_array).is_none() {
                            task["details"] = json!([]);
                        }
                        if let Some(details) = task.get_mut("details").and_then(Value::as_array_mut) {
                            details.push(Value::String(note.to_string()));
                        }
                    }
                    if !files.is_empty() {
                        if task.get("files").and_then(Value::as_array).is_none() {
                            task["files"] = json!([]);
                        }
                        if let Some(existing) = task.get_mut("files").and_then(Value::as_array_mut) {
                            for file in files {
                                existing.push(Value::String(file.clone()));
                            }
                        }
                    }
                    found = true;
                    break 'outer;
                }
            }
        }
    }
    found
}

/// Adds a pending task to the plan's last section, creating one for an empty plan.
///
/// One definition of the shape of a task added to a plan, shared by the Architect's
/// Update Plan action and the result view's one-click "Add to Plan", so the two
/// callers cannot drift about what a new milestone looks like.
pub fn append_pending_task(
    plan: &mut Value,
    title: &str,
    note: Option<&str>,
    tag: Option<&str>,
) -> Result<Value, String> {
    let steps_len = plan.get("steps").and_then(Value::as_array).map(|s| s.len()).unwrap_or(0);

    let has_section = plan
        .get("sections")
        .and_then(Value::as_array)
        .map(|sections| !sections.is_empty())
        .unwrap_or(false);
    if !has_section {
        if plan.get("sections").and_then(Value::as_array).is_none() {
            plan["sections"] = json!([]);
        }
        // A plan with no sections yet needs its first section created and *attached*:
        // the flattened `steps` view is derived from `sections`, so a bare local object
        // would be written to and then discarded.
        if let Some(sections) = plan.get_mut("sections").and_then(Value::as_array_mut) {
            sections.push(json!({"id": "sec-1", "title": "General", "tasks": []}));
        }
    }

    let section_title = plan
        .get("sections")
        .and_then(Value::as_array)
        .and_then(|sections| sections.last())
        .and_then(|section| section.get("title"))
        .cloned()
        .unwrap_or(Value::Null);

    let task = json!({
        "id": format!("task-{}", steps_len + 1),
        "section": section_title,
        "title": title,
        "status": "pending",
        "tag": tag.map(|tag| Value::String(tag.to_string())).unwrap_or(Value::Null),
        "details": note.map(|note| json!([note])).unwrap_or_else(|| json!([])),
        "files": [],
        // The same keys, in the same order, that the parser writes, so a task added
        // here is shaped exactly like one read out of the markdown.
        "behavioral_log": [],
        "sub_steps": [],
    });

    if let Some(sections) = plan.get_mut("sections").and_then(Value::as_array_mut) {
        if let Some(section) = sections.last_mut() {
            if section.get("tasks").and_then(Value::as_array).is_none() {
                section["tasks"] = json!([]);
            }
            if let Some(tasks) = section.get_mut("tasks").and_then(Value::as_array_mut) {
                tasks.push(task.clone());
            }
        }
    }

    Ok(task)
}

/// The AST structure verdict for a plan document.
pub fn plan_structure_report(content: &str) -> Value {
    check_plan_structure(content)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::Vocabulary;
    use std::collections::HashMap;

    fn vocabulary() -> Vocabulary {
        let mut canonical = HashMap::new();
        for tag in ["FE", "BE", "UI"] {
            canonical.insert(tag.to_string(), if tag == "UI" { "FE".to_string() } else { tag.to_string() });
        }
        Vocabulary {
            canonical,
            ui_tag: "FE".to_string(),
            ui_keyword_pattern: r"\b(?:ui|frontend|interface|view)s?\b".to_string(),
        }
    }

    struct Scratch {
        dir: PathBuf,
    }

    impl Scratch {
        fn new(name: &str) -> Scratch {
            let dir = std::env::temp_dir().join(format!("deepagents_core_{}_{}", name, std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).expect("scratch dir");
            Scratch { dir }
        }

        fn markdown(&self) -> PathBuf {
            self.dir.join("PLAN.md")
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn path_str(path: &Path) -> String {
        path.to_string_lossy().to_string()
    }

    /// The two halves of a save, as the Python wrapper performs them: derive the
    /// canonical state, then write the markdown projection.
    fn save(scratch: &Scratch, plan: &mut Value, vocabulary: &Vocabulary) {
        prepare_plan_state(plan, "PLAN.md").expect("prepare");
        let markdown = crate::parser::compile_plan_json_to_markdown(plan, vocabulary);
        write_plan_markdown(&path_str(&scratch.markdown()), &markdown).expect("write");
    }

    #[test]
    fn saving_recomputes_metrics_and_renders_the_markdown_projection() {
        let scratch = Scratch::new("save");
        let mut plan = json!({
            "title": "Demo",
            "sections": [{"id": "sec-1", "title": "1. Build", "tasks": [
                {"id": "task-1", "title": "A", "status": "completed", "tag": "BE", "details": [], "files": [], "behavioral_log": [], "sub_steps": []},
                {"id": "task-2", "title": "B", "status": "pending", "tag": "FE", "details": [], "files": [], "behavioral_log": [], "sub_steps": []},
            ]}],
        });
        save(&scratch, &mut plan, &vocabulary());

        assert_eq!(plan["metrics"]["total_tasks"], 2);
        assert_eq!(plan["metrics"]["completed_tasks"], 1);
        assert_eq!(plan["metrics"]["progress_percent"], 50);
        assert_eq!(plan["steps"].as_array().expect("steps").len(), 2);
        assert_eq!(plan["steps"][0]["section"], "1. Build");

        let markdown = read_plan_markdown(&path_str(&scratch.markdown()));
        assert!(markdown.contains("# Project Plan: Demo"));
        assert!(markdown.contains("- [x] [BE] A"));
        assert!(markdown.contains("- [ ] [FE] B"));
    }

    #[test]
    fn a_flat_plan_is_regrouped_rather_than_wiped() {
        let scratch = Scratch::new("flat");
        let mut plan = json!({
            "title": "Flat",
            "sections": [],
            "steps": [
                {"id": "task-1", "title": "A", "status": "pending", "section": "1. Build", "details": [], "files": [], "behavioral_log": [], "sub_steps": []},
                {"id": "task-2", "title": "B", "status": "pending", "section": "1. Build", "details": [], "files": [], "behavioral_log": [], "sub_steps": []},
                {"id": "task-3", "title": "C", "status": "pending", "section": "2. Ship", "details": [], "files": [], "behavioral_log": [], "sub_steps": []},
            ],
        });
        save(&scratch, &mut plan, &vocabulary());
        assert_eq!(plan["sections"].as_array().expect("sections").len(), 2);
        assert_eq!(plan["metrics"]["total_tasks"], 3);
    }

    #[test]
    fn appending_a_task_lands_in_the_last_section() {
        let mut plan = json!({
            "title": "Demo",
            "sections": [{"id": "sec-1", "title": "1. Build", "tasks": []}],
            "steps": [{"id": "task-1", "title": "A", "status": "pending"}],
        });
        let task = append_pending_task(&mut plan, "New one", Some("a note"), Some("BE")).expect("append");
        assert_eq!(task["id"], "task-2");
        assert_eq!(task["section"], "1. Build");
        assert_eq!(task["status"], "pending");
        assert_eq!(task["details"][0], "a note");
        assert_eq!(plan["sections"][0]["tasks"][0]["title"], "New one");
    }

    #[test]
    fn appending_to_an_empty_plan_creates_a_general_section() {
        let mut plan = json!({"title": "Empty", "sections": [], "steps": []});
        let task = append_pending_task(&mut plan, "First", None, None).expect("append");
        assert_eq!(task["id"], "task-1");
        assert_eq!(task["section"], "General");
        assert_eq!(plan["sections"][0]["id"], "sec-1");
    }

    #[test]
    fn updating_a_status_finds_a_task_by_title_too() {
        let scratch = Scratch::new("status");
        let mut plan = crate::parser::parse_markdown_to_plan_dict(
            "# Project Plan: Demo\n\n## 1. Build\n\n- [ ] A task\n- [ ] Another\n",
            "PLAN.md",
            false,
            &vocabulary(),
        );
        assert!(update_plan_task(&mut plan, "A task", "completed", Some("done by hand"), &["a.py".to_string()]));
        save(&scratch, &mut plan, &vocabulary());
        let task = &plan["steps"][0];
        assert_eq!(task["status"], "completed");
        assert_eq!(task["details"][0], "done by hand");
        assert_eq!(task["files"][0], "a.py");
        let markdown = read_plan_markdown(&path_str(&scratch.markdown()));
        assert!(markdown.contains("- [x] A task"));
    }

    #[test]
    fn the_lock_file_stays_out_of_the_plan_directory() {
        // The plan directory is the user's repository, so nothing the core needs for
        // locking may appear in it: it would be reported as an untracked file by the
        // codebase audit and shown in the file explorer.
        let scratch = Scratch::new("lockplace");
        {
            let _guard = PlanLock::acquire(&scratch.dir).expect("lock");
            let entries: Vec<String> = std::fs::read_dir(&scratch.dir)
                .expect("dir")
                .filter_map(|entry| entry.ok())
                .map(|entry| entry.file_name().to_string_lossy().to_string())
                .collect();
            assert!(entries.is_empty(), "plan directory holds {:?}", entries);
        }
    }

    #[test]
    fn a_write_publishes_through_a_rename_so_readers_never_see_a_half_file() {
        let scratch = Scratch::new("atomic");
        let target = scratch.dir.join("out.txt");
        atomic_write(&target, "first").expect("write");
        assert_eq!(std::fs::read_to_string(&target).expect("read"), "first");
        atomic_write(&target, "second").expect("overwrite");
        assert_eq!(std::fs::read_to_string(&target).expect("read"), "second");
        // No temporary files are left behind.
        let leftovers: Vec<String> = std::fs::read_dir(&scratch.dir)
            .expect("dir")
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .filter(|name| name.contains(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "leftovers: {:?}", leftovers);
    }
}
