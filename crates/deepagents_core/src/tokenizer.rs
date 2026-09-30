//! A markdown-it-shaped token stream, built on `pulldown-cmark`.
//!
//! The plan parser is a port of a walker that ran over `markdown-it-py`'s flat
//! token list, and it depends on two things that token list gave it: the exact
//! order of `list_item_open` / `inline` / `list_item_close` tokens, and the *raw
//! markdown source* of each inline block. The parser matches regular expressions
//! against that raw text (`- [x]`, ``Files: `a.py` ``, `🟢 Behavioral Log:`), so
//! a token stream reconstructed from rendered events would silently change what
//! the parser recognises.
//!
//! `pulldown-cmark` reports byte offsets for every event, so both properties can
//! be recovered: this module walks its events and re-emits them as a flat,
//! markdown-it-shaped list whose `Inline` tokens carry a slice of the original
//! source. Inline markup is therefore preserved exactly -- backticks, emphasis
//! markers and all -- because it is never rendered in the first place.
//!
//! Only four token kinds are produced, because only four are read: a heading
//! open/close pair (with its level), an inline text block, and the list item
//! open/close pair that gives the tree its shape.

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};

/// The token kinds the plan parser reads. Named after their markdown-it
/// counterparts so the ported walker can be compared against the original.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    HeadingOpen,
    HeadingClose,
    Inline,
    ListItemOpen,
    ListItemClose,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Token {
    pub kind: Kind,
    /// `"h1"`/`"h2"`/`"h3"` for [`Kind::HeadingOpen`], empty otherwise.
    pub tag: &'static str,
    /// Raw source text, for [`Kind::Inline`].
    pub text: String,
}

impl Token {
    fn plain(kind: Kind) -> Self {
        Token { kind, tag: "", text: String::new() }
    }

    fn heading_open(level: HeadingLevel) -> Self {
        let tag = match level {
            HeadingLevel::H1 => "h1",
            HeadingLevel::H2 => "h2",
            HeadingLevel::H3 => "h3",
            HeadingLevel::H4 => "h4",
            HeadingLevel::H5 => "h5",
            HeadingLevel::H6 => "h6",
        };
        Token { kind: Kind::HeadingOpen, tag, text: String::new() }
    }

    fn inline(text: String) -> Self {
        Token { kind: Kind::Inline, tag: "", text }
    }
}

/// The markdown-it preset this parser was written against is `gfm-like`, which
/// adds tables and strikethrough and nothing else. Task lists are deliberately
/// *not* enabled: markdown-it leaves `[x]` in the text, and so must we, because
/// the checkbox mark is the parser's own status field.
fn options() -> Options {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_TABLES);
    options.insert(Options::ENABLE_STRIKETHROUGH);
    options
}

/// One open block during the walk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FrameKind {
    /// A list item. In a *tight* list `pulldown-cmark` emits the item's text with
    /// no `Paragraph` wrapper, so an item frame has to be able to hold content.
    Item,
    Paragraph,
    Heading(HeadingLevel),
    TableCell,
}

struct Frame {
    kind: FrameKind,
    /// Byte range of the content seen so far, as `(min start, max end)`.
    start: usize,
    end: usize,
    has_content: bool,
}

/// Splits a source range into the text markdown-it would have reported for the
/// block, then trims it the way the walker does.
fn slice(source: &str, start: usize, end: usize) -> String {
    if end <= start {
        return String::new();
    }
    source[start..end].trim().to_string()
}

/// The text of an ATX or setext heading, without its markers.
///
/// `pulldown-cmark`'s heading range covers the markers as well as the text, and
/// markdown-it reports only the text -- with the optional closing sequence of an
/// ATX heading removed. Both spellings are handled because a hand-written plan
/// may use either.
fn heading_text(source: &str, start: usize, end: usize) -> String {
    if end <= start {
        return String::new();
    }
    let raw = source[start..end].trim();
    if !raw.starts_with('#') {
        // Setext: the range covers the underline as well, so take the first line.
        return raw.lines().next().unwrap_or("").trim().to_string();
    }
    let body = raw.trim_start_matches('#').trim();
    let body = body.trim_end();
    // A closing sequence (`## Title ##`) is only one when a space separates it
    // from the text; `## C#` keeps its hash.
    let hashes = body.len() - body.trim_end_matches('#').len();
    if hashes > 0 {
        let head = &body[..body.len() - hashes];
        if head.ends_with(char::is_whitespace) {
            return head.trim_end().to_string();
        }
    }
    body.to_string()
}

/// Whether a tag opens a block-level element.
fn is_block(tag: &Tag<'_>) -> bool {
    matches!(
        tag,
        Tag::Paragraph
            | Tag::Heading { .. }
            | Tag::BlockQuote(_)
            | Tag::CodeBlock(_)
            | Tag::HtmlBlock
            | Tag::List(_)
            | Tag::Item
            | Tag::FootnoteDefinition(_)
            | Tag::Table(_)
            | Tag::TableHead
            | Tag::TableRow
            | Tag::TableCell
            | Tag::DefinitionList
            | Tag::DefinitionListTitle
            | Tag::DefinitionListDefinition
            | Tag::MetadataBlock(_)
    )
}

/// Emits the pending inline content of an item frame, if it has any.
///
/// A tight list item's text arrives with no `Paragraph` wrapper, so it is held on
/// the item frame until the next block boundary (a nested list, say) or the item's
/// own end says where the content stopped.
fn flush_item_content(frame: &mut Frame, source: &str, tokens: &mut Vec<Token>) {
    if frame.kind == FrameKind::Item && frame.has_content {
        tokens.push(Token::inline(slice(source, frame.start, frame.end)));
        frame.has_content = false;
    }
}

/// Walks `source` and returns the flat token list the plan parser reads.
pub fn tokenize(source: &str) -> Vec<Token> {
    let mut tokens: Vec<Token> = Vec::new();
    let mut frames: Vec<Frame> = Vec::new();

    for (event, range) in Parser::new_ext(source, options()).into_offset_iter() {
        match event {
            Event::Start(Tag::Item) => {
                if let Some(frame) = frames.last_mut() {
                    flush_item_content(frame, source, &mut tokens);
                }
                frames.push(Frame {
                    kind: FrameKind::Item,
                    start: range.start,
                    end: range.start,
                    has_content: false,
                });
                tokens.push(Token::plain(Kind::ListItemOpen));
            }
            Event::End(TagEnd::Item) => {
                if let Some(mut frame) = frames.pop() {
                    flush_item_content(&mut frame, source, &mut tokens);
                }
                tokens.push(Token::plain(Kind::ListItemClose));
            }
            Event::Start(Tag::Paragraph) => {
                if let Some(frame) = frames.last_mut() {
                    flush_item_content(frame, source, &mut tokens);
                }
                frames.push(Frame {
                    kind: FrameKind::Paragraph,
                    start: range.start,
                    end: range.start,
                    has_content: false,
                });
            }
            Event::End(TagEnd::Paragraph) => {
                if let Some(frame) = frames.pop() {
                    if frame.has_content {
                        tokens.push(Token::inline(slice(source, frame.start, frame.end)));
                    }
                }
            }
            Event::Start(Tag::Heading { level, .. }) => {
                if let Some(frame) = frames.last_mut() {
                    flush_item_content(frame, source, &mut tokens);
                }
                frames.push(Frame {
                    kind: FrameKind::Heading(level),
                    start: range.start,
                    end: range.start,
                    has_content: false,
                });
                tokens.push(Token::heading_open(level));
            }
            Event::End(TagEnd::Heading(_)) => {
                if let Some(frame) = frames.pop() {
                    tokens.push(Token::inline(heading_text(source, frame.start, frame.end)));
                }
                tokens.push(Token::plain(Kind::HeadingClose));
            }
            Event::Start(Tag::TableCell) => {
                if let Some(frame) = frames.last_mut() {
                    flush_item_content(frame, source, &mut tokens);
                }
                frames.push(Frame {
                    kind: FrameKind::TableCell,
                    start: range.start,
                    end: range.start,
                    has_content: false,
                });
            }
            Event::End(TagEnd::TableCell) => {
                if let Some(frame) = frames.pop() {
                    if frame.has_content {
                        tokens.push(Token::inline(slice(source, frame.start, frame.end)));
                    }
                }
            }
            Event::Start(tag) => {
                if is_block(&tag) {
                    if let Some(frame) = frames.last_mut() {
                        flush_item_content(frame, source, &mut tokens);
                    }
                }
                // Inline-level tags are part of the surrounding content and only
                // widen its range, which the `_` arm below does.
                if !is_block(&tag) {
                    record_content(&mut frames, range.start, range.end);
                }
            }
            Event::End(tag_end) => {
                let block = !matches!(
                    tag_end,
                    TagEnd::Emphasis
                        | TagEnd::Strong
                        | TagEnd::Strikethrough
                        | TagEnd::Superscript
                        | TagEnd::Subscript
                        | TagEnd::Link
                        | TagEnd::Image
                );
                if !block {
                    record_content(&mut frames, range.start, range.end);
                }
            }
            Event::Rule => {
                if let Some(frame) = frames.last_mut() {
                    flush_item_content(frame, source, &mut tokens);
                }
            }
            // Every other event is inline-level content: text, code spans, line
            // breaks and raw inline HTML all widen the block they sit in.
            _ => record_content(&mut frames, range.start, range.end),
        }
    }

    tokens
}

fn record_content(frames: &mut [Frame], start: usize, end: usize) {
    if let Some(frame) = frames.last_mut() {
        if !frame.has_content {
            frame.start = start;
            frame.end = end;
            frame.has_content = true;
            return;
        }
        frame.start = frame.start.min(start);
        frame.end = frame.end.max(end);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(tokens: &[Token]) -> Vec<Kind> {
        tokens.iter().map(|t| t.kind).collect()
    }

    #[test]
    fn tight_item_yields_open_inline_close() {
        let tokens = tokenize("- [ ] Build the endpoint\n");
        assert_eq!(
            kinds(&tokens),
            vec![Kind::ListItemOpen, Kind::Inline, Kind::ListItemClose]
        );
        assert_eq!(tokens[1].text, "[ ] Build the endpoint");
    }

    #[test]
    fn nested_items_keep_document_order() {
        let tokens = tokenize("- [ ] Parent\n  - [ ] Sub one\n  - [ ] Sub two\n");
        assert_eq!(
            kinds(&tokens),
            vec![
                Kind::ListItemOpen,
                Kind::Inline,
                Kind::ListItemOpen,
                Kind::Inline,
                Kind::ListItemClose,
                Kind::ListItemOpen,
                Kind::Inline,
                Kind::ListItemClose,
                Kind::ListItemClose,
            ]
        );
        let texts: Vec<&str> = tokens
            .iter()
            .filter(|t| t.kind == Kind::Inline)
            .map(|t| t.text.as_str())
            .collect();
        assert_eq!(texts, vec!["[ ] Parent", "[ ] Sub one", "[ ] Sub two"]);
    }

    #[test]
    fn inline_markup_survives_as_source() {
        let tokens = tokenize("- [ ] Wire `ui/js/state.js` and **bold** text\n");
        assert_eq!(tokens[1].text, "[ ] Wire `ui/js/state.js` and **bold** text");
    }

    #[test]
    fn heading_reports_text_without_markers() {
        let tokens = tokenize("## Section ##\n");
        assert_eq!(kinds(&tokens), vec![Kind::HeadingOpen, Kind::Inline, Kind::HeadingClose]);
        assert_eq!(tokens[0].tag, "h2");
        assert_eq!(tokens[1].text, "Section");
    }

    #[test]
    fn heading_keeps_a_hash_that_is_not_a_closing_sequence() {
        let tokens = tokenize("## C# support\n");
        assert_eq!(tokens[1].text, "C# support");
    }

    #[test]
    fn loose_item_text_arrives_through_its_paragraph() {
        let tokens = tokenize("- [ ] Parent\n\n  More detail\n");
        let texts: Vec<&str> = tokens
            .iter()
            .filter(|t| t.kind == Kind::Inline)
            .map(|t| t.text.as_str())
            .collect();
        assert_eq!(texts, vec!["[ ] Parent", "More detail"]);
    }

    #[test]
    fn detail_bullet_is_its_own_item() {
        let tokens = tokenize("- [ ] Parent\n  - detail\n");
        let texts: Vec<&str> = tokens
            .iter()
            .filter(|t| t.kind == Kind::Inline)
            .map(|t| t.text.as_str())
            .collect();
        assert_eq!(texts, vec!["[ ] Parent", "detail"]);
    }

    #[test]
    fn thematic_break_does_not_become_content() {
        let tokens = tokenize("## Summary\n\n- one\n\n---\n\n## Next\n");
        let texts: Vec<&str> = tokens
            .iter()
            .filter(|t| t.kind == Kind::Inline)
            .map(|t| t.text.as_str())
            .collect();
        assert_eq!(texts, vec!["Summary", "one", "Next"]);
    }
}
