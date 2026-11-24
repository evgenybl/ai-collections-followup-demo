"""Run every test of this repository, with nothing installed: python -I run_tests.py

It finds the test_*.py files beside it and runs them with unittest; each file also runs on its own (python -I
test_pipeline.py, and so on). make_examples.py writes docs/TESTS.md from markdown() below.
Exit codes: 0 every test passed; 1 a test failed.
"""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def suite():
    return unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))


def cases(tests):
    """Every test of a suite, in the order unittest runs them."""
    for item in tests:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def first_paragraph(doc):
    """A docstring's first paragraph, without its "Run:" line, on one line."""
    text = (doc or "").split("\n\n")[0]
    return " ".join(line.strip() for line in text.splitlines() if not line.strip().startswith("Run:"))


def markdown():
    """docs/TESTS.md: every test, by file and class, its name written as a sentence."""
    found = list(cases(suite()))
    files = {}
    for test in found:
        files.setdefault(sys.modules[type(test).__module__], {}).setdefault(type(test), []).append(
            test._testMethodName)
    lines = ["# The tests", "",
             "Written by `python -I make_examples.py` from the names of the tests; do not edit it by hand. Run every "
             "test with `python -I run_tests.py`; each file also runs on its own.", "",
             f"{len(found)} tests in {len(files)} files.", ""]
    for module, classes in files.items():
        lines += [f"## {pathlib.Path(module.__file__).name}", "", first_paragraph(module.__doc__), ""]
        for cls, names in classes.items():
            lines += [f"### {cls.__name__}", ""]
            if cls.__doc__:
                lines += [first_paragraph(cls.__doc__), ""]
            lines += [f"- {name[len('test_'):].replace('_', ' ')}" for name in names] + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main():
    result = unittest.TextTestRunner(verbosity=1).run(suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
