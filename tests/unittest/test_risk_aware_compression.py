"""Risk-aware packing keeps important changed files visible under a fixed token budget."""

import pr_agent.algo.pr_processing as pr_processing
import pr_agent.algo.token_budget as token_budget
from pr_agent.algo.deterministic_risk import build_risk_checks
from pr_agent.algo.pr_processing import pr_generate_compressed_diff
from pr_agent.algo.review_file_priority import build_review_file_priority
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


class CharacterTokenHandler:
    prompt_tokens = 0

    def count_tokens(self, text):
        return len(text)


class FakeProvider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files

    def get_languages(self):
        return {"Python": 100}

    def get_filtered_diff_file_names(self):
        return []


def _file(name, line):
    patch = f"@@ -0,0 +1 @@\n+{line}\n"
    return FilePatchInfo(base_file="", head_file=line, patch=patch, filename=name, edit_type=EDIT_TYPE.ADDED)


def _packed(files, budget, priority=None):
    return pr_generate_compressed_diff(
        [{"language": "Python", "files": files}],
        CharacterTokenHandler(), budget, budget, False, False, file_priority=priority,
    )


def _rendered_size(name, patch):
    return len(f"\n\n## File: '{name}'\n\n{patch.strip()}\n")


def test_risk_priority_changes_coverage_without_raising_token_budget():
    documentation = _file("docs/long.md", "documentation " * 6)
    migration = _file("db/migrations/001.sql", "DROP TABLE users;")
    test = _file("tests/test_001.py", "assert migrated_schema()")
    ordinary = _file("src/ordinary.py", "return 1")
    files = [documentation, migration, test, ordinary]

    risk_checks = build_risk_checks(files)
    priority = build_review_file_priority(files, risk_checks)
    assert priority[migration.filename] == 3
    assert priority[test.filename] == 1

    _, _, _, _, file_dict, _ = _packed(files, 10_000)
    budget = (
        _rendered_size(migration.filename, file_dict[migration.filename]["patch"])
        + _rendered_size(test.filename, file_dict[test.filename]["patch"])
        + 1
    )
    assert _rendered_size(documentation.filename, file_dict[documentation.filename]["patch"]) < budget

    old_chunks, _, _, old_remaining, _, _ = _packed(files, budget)
    new_chunks, _, _, new_remaining, _, _ = _packed(files, budget, priority)

    assert "docs/long.md" in "\n".join(old_chunks[0])
    assert "db/migrations/001.sql" not in "\n".join(old_chunks[0])
    assert "db/migrations/001.sql" in "\n".join(new_chunks[0])
    assert "tests/test_001.py" in "\n".join(new_chunks[0])
    assert "docs/long.md" in new_remaining
    assert "db/migrations/001.sql" in old_remaining
    assert len("\n".join(new_chunks[0])) <= budget


def test_renamed_sensitive_file_and_matching_test_keep_priority():
    source = _file("src/auth.py", "return check_permission()")
    source.old_filename = ".github/workflows/auth.py"
    test = _file("tests/test_auth.py", "assert check_permission()")
    priority = build_review_file_priority([source, test], build_risk_checks([source, test]))

    assert priority == {"src/auth.py": 2, "tests/test_auth.py": 1}


def test_renamed_risky_file_keeps_test_matched_to_previous_stem():
    """A rename that changes the stem must still prioritize the old-name test."""
    source = _file("src/helpers.py", "api_key = 'secret'")
    source.old_filename = "src/auth.py"
    test = _file("tests/test_auth.py", "assert check_permission()")
    priority = build_review_file_priority([source, test], build_risk_checks([source, test]))

    assert priority["src/helpers.py"] == 3
    assert priority["tests/test_auth.py"] == 1


def test_conventional_js_and_go_tests_match_risky_sources():
    js_source = _file("src/auth.ts", "api_key = 'secret'")
    js_test = _file("src/auth.test.ts", "expect(true)")
    go_source = _file("pkg/auth/auth.go", "api_key = 'secret'")
    go_test = _file("pkg/auth/auth_test.go", "t.Fatal()")

    js_priority = build_review_file_priority([js_source, js_test], build_risk_checks([js_source, js_test]))
    go_priority = build_review_file_priority([go_source, go_test], build_risk_checks([go_source, go_test]))

    assert js_priority == {"src/auth.ts": 3, "src/auth.test.ts": 1}
    assert go_priority == {"pkg/auth/auth.go": 3, "pkg/auth/auth_test.go": 1}


def test_prepared_and_fresh_chunking_keep_the_same_priority(monkeypatch):
    files = [
        _file("src/ordinary.py", "ordinary " * 12),
        _file("db/migrations/002.sql", "DROP TABLE accounts;"),
        _file("tests/test_002.py", "assert migrated_accounts()"),
    ]
    provider = FakeProvider(files)
    priority = build_review_file_priority(files, build_risk_checks(files))
    handler = CharacterTokenHandler()
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, **kwargs: 1_690)

    prepared = pr_processing.get_pr_diff(
        provider, handler, "model", add_line_numbers_to_hunks=True,
        return_prepared=True, file_priority=priority,
    )
    reused = pr_processing.get_pr_multi_diffs(
        provider, handler, "model", max_calls=2, prepared_diff=prepared,
        file_priority=priority, return_coverage=True,
    )
    fresh = pr_processing.get_pr_multi_diffs(
        provider, handler, "model", max_calls=2, file_priority=priority, return_coverage=True,
    )

    assert isinstance(prepared, pr_processing.PreparedPRDiff)
    assert list(prepared.file_dict)[0] == "db/migrations/002.sql"
    assert "db/migrations/002.sql" in reused.chunks[0]
    assert "db/migrations/002.sql" in fresh.chunks[0]
    assert reused.remaining_files_list == fresh.remaining_files_list


def test_priority_does_not_change_a_review_when_every_file_fits(monkeypatch):
    files = [_file("src/ordinary.py", "return 1"), _file("db/migrations/003.sql", "DROP TABLE old_users;")]
    provider = FakeProvider(files)
    priority = build_review_file_priority(files, build_risk_checks(files))
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, **kwargs: 10_000)

    baseline = pr_processing.get_pr_diff(provider, CharacterTokenHandler(), "model")
    prioritized = pr_processing.get_pr_diff(
        provider, CharacterTokenHandler(), "model", file_priority=priority,
    )

    assert prioritized == baseline
