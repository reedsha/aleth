"""AST-driven structural chunking (Phase 1 of the context rebuild).

These tests pin the two properties the extractor exists to provide: a unit is a
*structurally complete* node (a whole class / interface / method / function, never a
line window that cuts one in half), and its byte span is *exact* for its file path -- so
``content`` always equals the file's own bytes over ``[byte_start, byte_end)``.

They also pin the JSON boundary: a ``CodeUnit`` is a strict Pydantic model
(``extra="forbid"``), so a shape that drifts is refused rather than carried quietly.

    .\\venv\\Scripts\\python.exe -m unittest tests.test_ast_chunker -v
"""

import json
import os
import tempfile
import unittest

from pydantic import ValidationError

from tools.ast_chunker import (
    CodeUnit,
    CodeUnitEnvelope,
    chunk_file,
    chunk_source,
    detect_language,
    units_from_json,
    units_to_json,
)


def _slc(source, unit):
    """The file's own bytes over the unit's span, exactly as ``content`` should be."""
    return source.encode("utf-8")[unit.byte_start:unit.byte_end].decode("utf-8")


def _line_of(source, needle):
    return source[: source.index(needle)].count("\n") + 1


def _col_of(source, needle):
    index = source.index(needle)
    return index - (source.rfind("\n", 0, index) + 1)


def _by_qualified(units, qualified):
    for unit in units:
        if unit.qualified_name == qualified:
            return unit
    raise AssertionError(f"no unit {qualified!r} in {[u.qualified_name for u in units]}")


PYTHON_SAMPLE = '''"""Module docstring."""

import os


def module_function(a, b):
    return a + b


class Service:
    def __init__(self, name):
        self.name = name

    def run(self):
        def closure():
            return self.name
        return closure
'''

JAVA_SAMPLE = '''public interface Greeter {
    String greet(String name);
}

public class Impl implements Greeter {
    public Impl() {
    }

    public String greet(String name) {
        return "hi " + name;
    }
}
'''

CSHARP_SAMPLE = '''public interface IGreeter {
    string Greet(string name);
}

public class Impl : IGreeter {
    public string Greet(string name) {
        return "hi " + name;
    }
}
'''

TYPESCRIPT_SAMPLE = '''export interface Greeter {
    greet(name: string): string;
}

export function hello(name: string): string {
    return name;
}

export class Impl {
    greet(name: string): string {
        return name;
    }
}
'''


class LanguageDetectionTests(unittest.TestCase):
    def test_known_extensions_map_to_their_language(self):
        self.assertEqual(detect_language("a.py"), "python")
        self.assertEqual(detect_language("A.java"), "java")
        self.assertEqual(detect_language("A.CS"), "csharp")
        self.assertEqual(detect_language("a.ts"), "typescript")
        self.assertEqual(detect_language("a.tsx"), "typescript")

    def test_unknown_extension_is_none(self):
        self.assertIsNone(detect_language("notes.txt"))
        self.assertIsNone(detect_language("noextension"))

    def test_extension_match_is_case_insensitive(self):
        self.assertEqual(detect_language("A.PY"), "python")


class PythonExtractionTests(unittest.TestCase):
    def test_module_level_function_is_a_function(self):
        units = chunk_source("def f(x):\n    return x\n", "a.py")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].kind, "function")
        self.assertEqual(units[0].qualified_name, "f")
        self.assertEqual(units[0].node_type, "function_definition")

    def test_class_and_methods_and_qualified_names(self):
        units = chunk_source(PYTHON_SAMPLE, "a.py")
        self.assertEqual(_by_qualified(units, "Service").kind, "class")
        self.assertEqual(_by_qualified(units, "Service.__init__").kind, "method")
        self.assertEqual(_by_qualified(units, "Service.run").kind, "method")
        self.assertEqual(_by_qualified(units, "module_function").kind, "function")

    def test_closure_inside_a_method_is_a_function_not_a_method(self):
        units = chunk_source(PYTHON_SAMPLE, "a.py")
        closure = _by_qualified(units, "Service.run.closure")
        self.assertEqual(closure.kind, "function")

    def test_nested_function_carries_its_enclosing_path(self):
        source = "def outer():\n    def inner():\n        pass\n    return inner\n"
        units = chunk_source(source, "a.py")
        self.assertEqual(_by_qualified(units, "outer.inner").kind, "function")

    def test_nested_class_carries_its_enclosing_path(self):
        source = "class Outer:\n    class Inner:\n        def m(self):\n            pass\n"
        units = chunk_source(source, "a.py")
        self.assertEqual(_by_qualified(units, "Outer.Inner").kind, "class")
        self.assertEqual(_by_qualified(units, "Outer.Inner.m").kind, "method")

    def test_async_function_is_a_function(self):
        source = "async def fetch(url):\n    return url\n"
        units = chunk_source(source, "a.py")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].kind, "function")
        self.assertEqual(units[0].node_type, "function_definition")

    def test_decorated_function_span_includes_the_decorator(self):
        source = "import functools\n\n\n@functools.cache\ndef compute():\n    return 1\n"
        unit = _by_qualified(chunk_source(source, "a.py"), "compute")
        self.assertTrue(unit.content.startswith("@functools.cache"))
        self.assertEqual(unit.byte_start, source.index("@functools.cache"))
        self.assertEqual(unit.node_type, "function_definition")

    def test_decorated_class_span_includes_the_decorator(self):
        source = "from dataclasses import dataclass\n\n\n@dataclass\nclass Point:\n    x: int\n"
        unit = _by_qualified(chunk_source(source, "a.py"), "Point")
        self.assertTrue(unit.content.startswith("@dataclass"))
        self.assertEqual(unit.node_type, "class_definition")

    def test_a_class_and_its_two_methods_yield_three_units(self):
        source = "class A:\n    def one(self):\n        pass\n\n    def two(self):\n        pass\n"
        units = chunk_source(source, "a.py")
        self.assertEqual([u.kind for u in units], ["class", "method", "method"])
        self.assertEqual([u.qualified_name for u in units], ["A", "A.one", "A.two"])

    def test_non_definitions_are_not_units(self):
        source = (
            "x = 1\n"
            "y = lambda a: a + 1\n"
            "print(x)\n"
            "class C:\n    attr = 2\n"
        )
        units = chunk_source(source, "a.py")
        self.assertEqual([u.qualified_name for u in units], ["C"])

    def test_empty_source_yields_no_units(self):
        self.assertEqual(chunk_source("", "a.py"), [])
        self.assertEqual(chunk_source("\n\n# just a comment\n", "a.py"), [])


class MultiLanguageTests(unittest.TestCase):
    def test_java_interface_class_method_and_constructor(self):
        units = chunk_source(JAVA_SAMPLE, "a.java")
        self.assertEqual(_by_qualified(units, "Greeter").kind, "interface")
        self.assertEqual(_by_qualified(units, "Impl").kind, "class")
        self.assertEqual(_by_qualified(units, "Impl.greet").kind, "method")
        # A constructor is a method in the sense the brief uses the word.
        self.assertEqual(_by_qualified(units, "Impl.Impl").kind, "method")

    def test_csharp_interface_class_and_method(self):
        units = chunk_source(CSHARP_SAMPLE, "a.cs")
        self.assertEqual(_by_qualified(units, "IGreeter").kind, "interface")
        self.assertEqual(_by_qualified(units, "Impl").kind, "class")
        self.assertEqual(_by_qualified(units, "Impl.Greet").kind, "method")

    def test_typescript_interface_function_class_and_methods(self):
        units = chunk_source(TYPESCRIPT_SAMPLE, "a.ts")
        self.assertEqual(_by_qualified(units, "Greeter").kind, "interface")
        self.assertEqual(_by_qualified(units, "Greeter.greet").kind, "method")
        self.assertEqual(_by_qualified(units, "hello").kind, "function")
        self.assertEqual(_by_qualified(units, "Impl").kind, "class")
        self.assertEqual(_by_qualified(units, "Impl.greet").kind, "method")


class ByteRangeAndPositionTests(unittest.TestCase):
    SAMPLES = [
        ("python", "a.py", PYTHON_SAMPLE),
        ("java", "a.java", JAVA_SAMPLE),
        ("csharp", "a.cs", CSHARP_SAMPLE),
        ("typescript", "a.ts", TYPESCRIPT_SAMPLE),
    ]

    def test_content_always_equals_the_file_slice(self):
        for language, filepath, source in self.SAMPLES:
            for unit in chunk_source(source, filepath):
                with self.subTest(language=language, unit=unit.qualified_name):
                    self.assertEqual(_slc(source, unit), unit.content)
                    self.assertEqual(unit.language, language)

    def test_spans_are_non_empty_and_ordered(self):
        for _, filepath, source in self.SAMPLES:
            units = chunk_source(source, filepath)
            starts = [u.byte_start for u in units]
            self.assertEqual(starts, sorted(starts), f"{filepath} not in source order")
            for unit in units:
                self.assertLess(unit.byte_start, unit.byte_end)
                self.assertLessEqual(unit.byte_end, len(source.encode("utf-8")))

    def test_line_numbers_are_one_based_and_exact(self):
        units = chunk_source(PYTHON_SAMPLE, "a.py")
        service = _by_qualified(units, "Service")
        self.assertEqual(service.line_start, _line_of(PYTHON_SAMPLE, "class Service:"))
        self.assertEqual(service.line_start, 10)
        run = _by_qualified(units, "Service.run")
        self.assertEqual(run.line_start, _line_of(PYTHON_SAMPLE, "def run(self):"))

    def test_columns_are_zero_based(self):
        units = chunk_source(PYTHON_SAMPLE, "a.py")
        run = _by_qualified(units, "Service.run")
        self.assertEqual(run.col_start, _col_of(PYTHON_SAMPLE, "def run(self):"))
        self.assertEqual(run.col_start, 4)

    def test_filepath_is_carried_on_every_unit(self):
        units = chunk_source(PYTHON_SAMPLE, "some/nested/path.py")
        self.assertTrue(units)
        for unit in units:
            self.assertEqual(unit.filepath, "some/nested/path.py")


class JsonBoundaryTests(unittest.TestCase):
    def test_envelope_round_trips(self):
        units = chunk_source(PYTHON_SAMPLE, "a.py")
        text = units_to_json("a.py", "python", units)
        parsed = units_from_json(text)
        self.assertIsInstance(parsed, CodeUnitEnvelope)
        self.assertEqual(parsed.filepath, "a.py")
        self.assertEqual(parsed.language, "python")
        self.assertEqual([u.content for u in parsed.units], [u.content for u in units])
        self.assertEqual([u.byte_start for u in parsed.units], [u.byte_start for u in units])

    def test_json_keys_are_the_contract(self):
        units = chunk_source("def f():\n    pass\n", "a.py")
        payload = json.loads(units_to_json("a.py", "python", units))
        self.assertEqual(
            set(payload["units"][0]),
            {
                "filepath", "language", "kind", "name", "qualified_name", "node_type",
                "byte_start", "byte_end", "line_start", "line_end", "col_start", "col_end",
                "content",
            },
        )

    def test_unit_refuses_an_unknown_field(self):
        with self.assertRaises(ValidationError):
            CodeUnit(
                filepath="a.py", language="python", kind="function", name="f",
                qualified_name="f", node_type="function_definition", byte_start=0,
                byte_end=1, line_start=1, line_end=1, col_start=0, col_end=0,
                content="f", surprise=1,
            )

    def test_envelope_refuses_an_unknown_field(self):
        with self.assertRaises(ValidationError):
            CodeUnitEnvelope(filepath="a.py", language="python", units=[], extra=1)

    def test_a_drifted_unit_in_json_is_refused(self):
        payload = json.loads(units_to_json("a.py", "python", chunk_source("def f():\n    pass\n", "a.py")))
        payload["units"][0]["invented"] = True
        with self.assertRaises(ValidationError):
            units_from_json(json.dumps(payload))

    def test_missing_required_fields_are_refused(self):
        payload = json.loads(units_to_json("a.py", "python", chunk_source("def f():\n    pass\n", "a.py")))
        del payload["units"][0]["byte_end"]
        with self.assertRaises(ValidationError):
            units_from_json(json.dumps(payload))


class DeterminismAndHygieneTests(unittest.TestCase):
    def test_repeated_extraction_is_stable(self):
        # Guards the tree-sitter lifetime: accessing node points must not corrupt state
        # across parses (an earlier tree-sitter build segfaulted on exactly this).
        runs = [chunk_source(PYTHON_SAMPLE, "a.py") for _ in range(5)]
        spans = [[(u.kind, u.byte_start, u.byte_end) for u in run] for run in runs]
        self.assertTrue(all(s == spans[0] for s in spans))

    def test_the_extractor_uses_no_regular_expressions(self):
        # The brief forbids regex for code parsing; parsing is tree-sitter's job alone.
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "ast_chunker.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("import re\n", "re.compile", "re.match", "re.search", "re.findall", "re.sub"):
            self.assertNotIn(banned, source)


class ErrorHandlingTests(unittest.TestCase):
    def test_unknown_extension_without_a_language_raises(self):
        with self.assertRaises(ValueError):
            chunk_source("hello", "notes.txt")

    def test_unsupported_language_raises(self):
        with self.assertRaises(ValueError):
            chunk_source("hello", "a.rb", language="ruby")

    def test_syntax_errors_do_not_raise(self):
        # tree-sitter recovers; a broken file must not crash the extractor.
        units = chunk_source("def f(:\n    return\n", "a.py")
        self.assertIsInstance(units, list)


class FileTests(unittest.TestCase):
    def test_chunk_file_reads_and_tags_the_file_path(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "sample.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("def solo():\n    return 1\n")
            units = chunk_file(path)
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0].filepath, path)
            self.assertEqual(units[0].qualified_name, "solo")

    def test_chunk_file_rejects_an_unknown_extension(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "sample.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hello")
            with self.assertRaises(ValueError):
                chunk_file(path)


if __name__ == "__main__":
    unittest.main()
