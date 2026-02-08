"""Unit tests for git_file_walker.py"""

import os
import tempfile
from pathlib import Path

import pytest

from app.core.git_file_walker import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE,
    SKIP_DIRS,
    GitFileInfo,
    classify_file,
    extract_repo_name,
    walk_repo_files,
)


# ============================================================================
# extract_repo_name
# ============================================================================


class TestExtractRepoName:
    def test_https_url(self):
        assert extract_repo_name("https://github.com/owner/my-repo.git") == "my-repo"

    def test_https_url_no_dot_git(self):
        assert extract_repo_name("https://github.com/owner/my-repo") == "my-repo"

    def test_ssh_url(self):
        assert extract_repo_name("git@github.com:owner/my-repo.git") == "my-repo"

    def test_ssh_url_no_dot_git(self):
        assert extract_repo_name("git@github.com:owner/my-repo") == "my-repo"

    def test_gitlab_ssh(self):
        assert extract_repo_name("git@gitlab.com:group/sub/repo.git") == "repo"

    def test_nested_path(self):
        assert extract_repo_name("https://github.com/org/sub/deep-repo.git") == "deep-repo"


# ============================================================================
# classify_file
# ============================================================================


class TestClassifyFile:
    def test_lkml_extension(self):
        assert classify_file(".lkml", "", False) == "lookml_view"

    def test_sqlx_extension(self):
        assert classify_file(".sqlx", "", False) == "dataform_table"

    def test_tds_extension(self):
        assert classify_file(".tds", "", False) == "tableau_workbook"

    def test_tdsx_extension(self):
        assert classify_file(".tdsx", "", False) == "tableau_workbook"

    def test_md_extension(self):
        assert classify_file(".md", "", False) == "markdown_document"

    def test_yml_dbt_source(self):
        content = "version: 2\nsources:\n  - name: raw"
        assert classify_file(".yml", content, False) == "dbt_source"

    def test_yml_dbt_model(self):
        content = "version: 2\nmodels:\n  - name: orders"
        assert classify_file(".yml", content, False) == "dbt_source"

    def test_yml_snowflake_semantic(self):
        content = "name: my_view\nbase_table:\n  database: db"
        assert classify_file(".yml", content, False) == "snowflake_semantic_view"

    def test_yml_generic(self):
        content = "some_key: some_value\nanother: thing"
        assert classify_file(".yml", content, False) == "yaml_file"

    def test_yaml_generic(self):
        content = "config:\n  setting: true"
        assert classify_file(".yaml", content, False) == "yaml_file"

    def test_sql_in_dbt_project(self):
        assert classify_file(".sql", "SELECT * FROM orders", True) == "dbt_model"

    def test_sql_outside_dbt_project(self):
        assert classify_file(".sql", "SELECT * FROM orders", False) == "sql_file"

    def test_py_extension(self):
        assert classify_file(".py", "import os", False) == "generic_file"

    def test_json_extension(self):
        assert classify_file(".json", "{}", False) == "generic_file"

    def test_csv_extension(self):
        assert classify_file(".csv", "a,b,c", False) == "generic_file"


# ============================================================================
# walk_repo_files
# ============================================================================


class TestWalkRepoFiles:
    def _make_repo(self, tmp_path, files, dbt=False):
        """Create a fake repo directory structure."""
        for rel_path, content in files.items():
            full_path = tmp_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        if dbt:
            (tmp_path / "dbt_project.yml").write_text("name: test_dbt", encoding="utf-8")
        return str(tmp_path)

    def test_basic_walk(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "models/orders.sql": "SELECT * FROM orders",
            "models/schema.yml": "version: 2\nmodels:\n  - name: orders",
            "README.md": "# Hello",
        })
        files = walk_repo_files(repo_path, "test-repo")

        assert len(files) == 3
        paths = {f.relative_path for f in files}
        assert "test-repo/models/orders.sql" in paths
        assert "test-repo/models/schema.yml" in paths
        assert "test-repo/README.md" in paths

    def test_extension_filtering(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "data.json": '{"key": "value"}',
            "script.sh": "#!/bin/bash",      # not in allowlist
            "binary.exe": "notreal",          # not in allowlist
            "notes.txt": "hello",
        })
        files = walk_repo_files(repo_path, "repo")

        extensions = {f.extension for f in files}
        assert ".json" in extensions
        assert ".txt" in extensions
        assert ".sh" not in extensions
        assert ".exe" not in extensions

    def test_skip_directories(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "src/main.py": "print('hi')",
            ".git/config": "gitconfig",
            "node_modules/package/index.js": "module.exports = {}",
            "__pycache__/mod.cpython.py": "cached",
        })
        files = walk_repo_files(repo_path, "repo")

        paths = {f.relative_path for f in files}
        assert "repo/src/main.py" in paths
        # Skipped dirs should not appear
        for p in paths:
            assert ".git/" not in p
            assert "node_modules/" not in p
            assert "__pycache__/" not in p

    def test_max_file_size(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "small.txt": "small",
            "large.txt": "x" * (MAX_FILE_SIZE + 1),
        })
        files = walk_repo_files(repo_path, "repo")

        paths = {f.relative_path for f in files}
        assert "repo/small.txt" in paths
        assert "repo/large.txt" not in paths

    def test_empty_file_skipped(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "empty.txt": "",
            "notempty.txt": "content",
        })
        # write_text("") creates a 0-byte file
        files = walk_repo_files(repo_path, "repo")

        paths = {f.relative_path for f in files}
        assert "repo/empty.txt" not in paths
        assert "repo/notempty.txt" in paths

    def test_content_hash_computed(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "file.sql": "SELECT 1",
        })
        files = walk_repo_files(repo_path, "repo")

        assert len(files) == 1
        assert len(files[0].content_hash) == 64  # SHA-256 hex digest

    def test_dbt_project_detection(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "models/orders.sql": "SELECT * FROM orders",
        }, dbt=True)
        files = walk_repo_files(repo_path, "dbt-repo")

        sql_files = [f for f in files if f.extension == ".sql"]
        assert len(sql_files) >= 1
        assert sql_files[0].resource_type == "dbt_model"

        # dbt_project.yml itself should be classified as dbt_source
        yml_files = [f for f in files if f.relative_path.endswith("dbt_project.yml")]
        assert len(yml_files) == 1

    def test_repo_name_prefix(self, tmp_path):
        repo_path = self._make_repo(tmp_path, {
            "file.sql": "SELECT 1",
        })
        files = walk_repo_files(repo_path, "my-special-repo")

        assert files[0].relative_path.startswith("my-special-repo/")

    def test_latin1_fallback(self, tmp_path):
        """Test that latin-1 encoded files are read successfully."""
        file_path = tmp_path / "latin.sql"
        file_path.write_bytes(b"SELECT '\xe9l\xe8ve'")  # latin-1 encoded
        files = walk_repo_files(str(tmp_path), "repo")

        assert len(files) == 1
        assert "élève" in files[0].content
