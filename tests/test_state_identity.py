"""State identity: a project's id must not be a function of where it happens to sit.

The failure this file exists to prevent: state keyed on an absolute path is orphaned the moment
the path changes -- a clone to another directory, a move, or a bind mount into a container. The
tests below are written as those scenarios rather than as unit assertions about a hash.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from tools import workspace


def git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_repo(path: str, remote: str = "") -> None:
    """A real repository, optionally with an origin."""
    subprocess.run(["git", "init", "-q", path], capture_output=True, timeout=60)
    if remote:
        subprocess.run(
            ["git", "-C", path, "remote", "add", "origin", remote],
            capture_output=True, timeout=60,
        )


class IdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_identity_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _dir(self, name: str) -> str:
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path


class ExplicitIdentityTests(IdentityTestCase):
    """Tier 1: the project's own declaration."""

    def test_an_id_file_wins_over_everything_else(self):
        project = self._dir("explicit")
        with open(os.path.join(project, workspace.IDENTITY_FILE), "w", encoding="utf-8") as handle:
            handle.write("my-stable-project\n")
        self.assertEqual(workspace.project_id(project), "my-stable-project")

    def test_the_id_survives_a_move(self):
        """The scenario: the same project at two different paths keeps one identity."""
        first, second = self._dir("before"), self._dir("after")
        for path in (first, second):
            with open(os.path.join(path, workspace.IDENTITY_FILE), "w", encoding="utf-8") as handle:
                handle.write("travelling-id\n")
        self.assertEqual(workspace.project_id(first), workspace.project_id(second))

    def test_an_unsafe_token_is_refused_rather_than_sanitised(self):
        """A mangled id would silently become a *different* project's state directory."""
        project = self._dir("unsafe")
        with open(os.path.join(project, workspace.IDENTITY_FILE), "w", encoding="utf-8") as handle:
            handle.write("../../escape\n")
        self.assertNotEqual(workspace.project_id(project), "../../escape")
        # It fell through to minting, which records a safe id instead.
        self.assertRegex(workspace.project_id(project), r"^[A-Za-z0-9._-]{1,64}$")


class RemoteNormalisationTests(unittest.TestCase):
    """One project, one identity -- whatever protocol it was cloned with."""

    def test_every_spelling_of_one_remote_is_one_string(self):
        expected = "github.com/org/repo"
        for spelling in (
            "https://github.com/org/repo.git",
            "https://github.com/org/repo",
            "https://github.com/org/repo/",
            "http://github.com/org/repo.git",
            "git@github.com:org/repo.git",
            "git@github.com:org/repo",
            "ssh://git@github.com/org/repo.git",
            "git://github.com/org/repo.git",
            "https://GitHub.com/org/repo.git",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(workspace.canonical_git_remote(spelling), expected)

    def test_different_projects_stay_different(self):
        first = workspace.canonical_git_remote("git@github.com:org/one.git")
        second = workspace.canonical_git_remote("https://github.com/org/two.git")
        third = workspace.canonical_git_remote("https://gitlab.com/org/one.git")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_an_unrecognised_spelling_is_still_normalised_and_stable(self):
        odd = "weird-spelling-without-a-path"
        self.assertEqual(workspace.canonical_git_remote(odd), odd.lower())
        self.assertEqual(workspace.canonical_git_remote(odd.upper()), odd.lower())


@unittest.skipUnless(git_available(), "git is not installed")
class ProtocolEquivalenceTests(IdentityTestCase):
    """The scenario: two developers clone the same repository over different protocols."""

    def test_ssh_and_https_clones_share_one_identity(self):
        ssh, https = self._dir("via-ssh"), self._dir("via-https")
        make_repo(ssh, "git@github.com:acme/thing.git")
        make_repo(https, "https://github.com/acme/thing.git")

        self.assertEqual(
            workspace.project_id(ssh), workspace.project_id(https),
            "one project cloned two ways got two identities; one developer's state is orphaned",
        )


@unittest.skipUnless(git_available(), "git is not installed")
class GitBoundIdentityTests(IdentityTestCase):
    """Tier 2: the repository's identity, which is not a location."""

    def test_the_same_repository_at_two_paths_has_one_identity(self):
        remote = "https://example.invalid/acme/thing.git"
        first, second = self._dir("clone-a"), self._dir("clone-b")
        make_repo(first, remote)
        make_repo(second, remote)

        self.assertEqual(workspace.project_id(first), workspace.project_id(second))
        # And it is not the path hash, which is what would have been orphaned.
        import hashlib

        path_hash = hashlib.sha256(first.encode()).hexdigest()[: workspace.PROJECT_ID_LENGTH]
        self.assertNotEqual(workspace.project_id(first), path_hash)

    def test_a_different_remote_is_a_different_project(self):
        first, second = self._dir("acme"), self._dir("other")
        make_repo(first, "https://example.invalid/acme/thing.git")
        make_repo(second, "https://example.invalid/other/thing.git")
        self.assertNotEqual(workspace.project_id(first), workspace.project_id(second))

    def test_a_repository_is_not_left_with_a_stray_id_file(self):
        """Tier 2 needs no file, so a real repository stays clean."""
        project = self._dir("clean")
        make_repo(project, "https://example.invalid/acme/thing.git")
        workspace.project_id(project)
        self.assertFalse(os.path.exists(os.path.join(project, workspace.IDENTITY_FILE)))


class MintedIdentityTests(IdentityTestCase):
    """Tier 3: no declaration and no repository -- mint one and record it."""

    def test_a_plain_directory_mints_and_records_an_id(self):
        project = self._dir("plain")
        minted = workspace.project_id(project)

        recorded = os.path.join(project, workspace.IDENTITY_FILE)
        self.assertTrue(os.path.isfile(recorded), "the id was not recorded")
        self.assertRegex(minted, r"^[A-Za-z0-9._-]{1,64}$")
        # Stable from then on: the recorded id is what answers next time.
        self.assertEqual(workspace.project_id(project), minted)

    def test_a_minted_id_is_added_to_an_existing_gitignore(self):
        project = self._dir("ignored")
        with open(os.path.join(project, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write("venv/\n")
        workspace.project_id(project)

        with open(os.path.join(project, ".gitignore"), "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn(workspace.IDENTITY_FILE, content.splitlines())
        self.assertIn("venv/", content.splitlines())

    def test_the_gitignore_is_not_appended_to_twice(self):
        project = self._dir("ignored-twice")
        with open(os.path.join(project, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".aleth_id\n")
        workspace.project_id(project)
        with open(os.path.join(project, ".gitignore"), "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(lines.count(workspace.IDENTITY_FILE), 1)


@unittest.skipUnless(os.name == "posix", "needs POSIX directory permissions")
class LastResortIdentityTests(IdentityTestCase):
    """Tier 4: nothing else is possible, and the answer is reported as the weak one it is."""

    def test_an_unwritable_directory_falls_back_to_its_path(self):
        import contextlib
        import io

        project = self._dir("frozen")
        os.chmod(project, 0o555)
        self.addCleanup(os.chmod, project, 0o755)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            identity = workspace.project_id(project)

        self.assertRegex(identity, r"^[A-Za-z0-9._-]{1,64}$")
        self.assertFalse(os.path.exists(os.path.join(project, workspace.IDENTITY_FILE)))
        self.assertIn("Falling back to its path", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
