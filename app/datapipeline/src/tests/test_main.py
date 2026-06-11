from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.datapipeline.src.domain.schemas import ExtractedQuestion, PolicySource

_URL = "https://defra.sharepoint.com/teams/T1/SitePages/Policy.aspx"
_TS = datetime(2024, 6, 1, tzinfo=timezone.utc)
_SOURCE = PolicySource(
    url_id=1,
    url=_URL,
    filename="Test policy",
    category="security",
    source="SharePoint",
    isactive=True,
)
_QUESTIONS = [
    ExtractedQuestion(
        question_text="Is data encrypted?",
        reference="Section 3.2",
        source_excerpt="Data must be encrypted.",
    )
]

_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "testdb",
    "DB_SCHEMA": "data_pipeline",
    "DB_USER": "user",
    "DB_PASSWORD": "test-db-password",
    "SHAREPOINT_TENANT_ID": "tid",
    "SHAREPOINT_CLIENT_ID": "cid",
    "SHAREPOINT_CLIENT_SECRET": "sec",
    "AWS_DEFAULT_REGION": "eu-west-2",
    "MODEL_ID": "anthropic.claude-3-5-sonnet-v2",
}


def _patch_pipeline(
    sources: list[PolicySource],
    content: str = "Policy text",
    last_modified: datetime | None = _TS,
    questions: list[ExtractedQuestion] | None = None,
    sync_changed: bool = True,
):
    """Context manager that patches all external dependencies of run()."""
    questions = questions if questions is not None else _QUESTIONS

    patches = {
        "app.datapipeline.src.entrypoints.main._get_db_connection": MagicMock(
            return_value=MagicMock()
        ),
        "app.datapipeline.src.entrypoints.main._build_sharepoint_client": MagicMock(),
        "app.datapipeline.src.entrypoints.main._build_extractor": MagicMock(),
        "app.datapipeline.src.entrypoints.main.fetch_policy_sources": MagicMock(
            return_value=sources
        ),
        "app.datapipeline.src.entrypoints.main.get_sync_record": MagicMock(
            return_value=None
        ),
        "app.datapipeline.src.entrypoints.main.is_changed": MagicMock(
            return_value=sync_changed
        ),
        "app.datapipeline.src.entrypoints.main.insert_policy_document": MagicMock(
            return_value="doc-uuid"
        ),
        "app.datapipeline.src.entrypoints.main.delete_questions_for_doc": MagicMock(
            return_value=0
        ),
        "app.datapipeline.src.entrypoints.main.insert_questions": MagicMock(
            return_value=len(questions)
        ),
        "app.datapipeline.src.entrypoints.main.upsert_sync_record": MagicMock(),
    }

    sp_mock = patches[
        "app.datapipeline.src.entrypoints.main._build_sharepoint_client"
    ].return_value
    sp_mock.return_value.read_page_content.return_value = (content, last_modified)

    extractor_mock = patches[
        "app.datapipeline.src.entrypoints.main._build_extractor"
    ].return_value
    extractor_mock.return_value.extract.return_value = (
        questions,
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "estimated_cost_usd": 0.0,
        },
    )

    return patches


@pytest.fixture(autouse=True)
def set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


class TestRunSummary:
    def test_processed_count(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            summary = run()

        assert summary["processed"] == 1
        assert summary["failed"] == 0
        assert summary["skipped"] == 0

    def test_skipped_when_content_unchanged(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sync_changed=False)
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["skipped"] == 1
        assert summary["processed"] == 0

    def test_failed_when_sharepoint_raises(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        mocks[
            "_build_sharepoint_client"
        ].return_value.read_page_content.side_effect = RuntimeError("SP down")
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["failed"] == 1
        assert summary["processed"] == 0

    def test_failed_when_llm_raises(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        mocks["_build_extractor"].return_value.extract.side_effect = ValueError(
            "Bad JSON"
        )
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["failed"] == 1

    def test_failed_when_llm_returns_empty(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(questions=[])
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["failed"] == 1

    def test_returns_all_zeros_when_no_sources(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[])
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary == {"processed": 0, "skipped": 0, "failed": 0, "cleaned": 0}

    def test_raises_when_env_var_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODEL_ID")
        from app.datapipeline.src.entrypoints.main import run

        with pytest.raises(
            RuntimeError, match="Missing required environment variable: MODEL_ID"
        ):
            run()

    def test_delete_called_before_insert_on_changed_page(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        call_order: list[str] = []
        mocks = _build_mocks()
        mocks["delete_questions_for_doc"].side_effect = lambda *_: call_order.append(
            "delete"
        )
        mocks["insert_questions"].side_effect = lambda *_: (
            call_order.append("insert") or 1
        )
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        assert call_order == ["delete", "insert"]

    def test_db_rollback_on_write_failure(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        mocks["insert_policy_document"].side_effect = Exception("DB connection lost")
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        mocks["_get_db_connection"].return_value.rollback.assert_called_once()
        assert summary["failed"] == 1


_INACTIVE_SOURCE = PolicySource(
    url_id=2,
    url="https://defra.sharepoint.com/teams/T1/SitePages/Old.aspx",
    filename="Old policy",
    category="technical",
    source="SharePoint",
    isactive=False,
)


class TestInactiveSourceCleanup:
    def test_inactive_source_increments_skipped(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[_INACTIVE_SOURCE])
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["skipped"] == 1
        assert summary["processed"] == 0

    def test_inactive_source_calls_delete_when_data_exists(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[_INACTIVE_SOURCE])
        mocks["delete_policy_document_by_url"] = MagicMock(return_value=1)
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        mocks["delete_policy_document_by_url"].assert_called_once_with(
            mocks["_get_db_connection"].return_value, _INACTIVE_SOURCE.url
        )
        assert summary["cleaned"] == 1

    def test_inactive_source_does_not_increment_cleaned_when_no_data(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[_INACTIVE_SOURCE])
        mocks["delete_policy_document_by_url"] = MagicMock(return_value=0)
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["cleaned"] == 0
        assert summary["skipped"] == 1

    def test_inactive_source_does_not_fetch_sharepoint(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[_INACTIVE_SOURCE])
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        mocks[
            "_build_sharepoint_client"
        ].return_value.read_page_content.assert_not_called()

    def test_mixed_sources_processed_and_cleaned(self) -> None:
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks(sources=[_SOURCE, _INACTIVE_SOURCE])
        mocks["delete_policy_document_by_url"] = MagicMock(return_value=1)
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            summary = run()

        assert summary["processed"] == 1
        assert summary["cleaned"] == 1
        assert summary["skipped"] == 1


class TestLocalSourcesFeatureFlag:
    def test_uses_local_sources_when_flag_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("USE_LOCAL_POLICY_SOURCES", "true")
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        local_mock = MagicMock(return_value=[_SOURCE])
        mocks["load_local_policy_sources"] = local_mock
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        local_mock.assert_called_once()
        mocks["fetch_all_policy_sources"].assert_not_called()

    def test_uses_db_sources_when_flag_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("USE_LOCAL_POLICY_SOURCES", "false")
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        local_mock = MagicMock(return_value=[_SOURCE])
        mocks["load_local_policy_sources"] = local_mock
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        mocks["fetch_all_policy_sources"].assert_called_once()
        local_mock.assert_not_called()

    def test_uses_db_sources_when_flag_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("USE_LOCAL_POLICY_SOURCES", raising=False)
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        local_mock = MagicMock(return_value=[_SOURCE])
        mocks["load_local_policy_sources"] = local_mock
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        mocks["fetch_all_policy_sources"].assert_called_once()
        local_mock.assert_not_called()

    def test_custom_path_passed_to_loader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.setenv("USE_LOCAL_POLICY_SOURCES", "true")
        monkeypatch.setenv("LOCAL_POLICY_SOURCES_PATH", "/custom/path/sources.json")
        from app.datapipeline.src.entrypoints.main import run

        mocks = _build_mocks()
        local_mock = MagicMock(return_value=[_SOURCE])
        mocks["load_local_policy_sources"] = local_mock
        with patch.multiple("app.datapipeline.src.entrypoints.main", **mocks):
            run()

        local_mock.assert_called_once_with("/custom/path/sources.json")


class TestDebugOutput:
    def test_debug_file_written_when_flag_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.setenv("SAVE_DEBUG_OUTPUT", "true")
        monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            run()

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".txt"

    def test_debug_file_contains_three_sections(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.setenv("SAVE_DEBUG_OUTPUT", "true")
        monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            run()

        text = list(tmp_path.iterdir())[0].read_text(encoding="utf-8")
        assert "=== SOURCE URL ===" in text
        assert "=== RAW CONTENT ===" in text
        assert "=== QUESTIONS GENERATED ===" in text
        assert _URL in text

    def test_debug_file_not_written_when_flag_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.setenv("SAVE_DEBUG_OUTPUT", "false")
        monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            run()

        assert list(tmp_path.iterdir()) == []

    def test_debug_file_not_written_when_flag_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.delenv("SAVE_DEBUG_OUTPUT", raising=False)
        monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(tmp_path))
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            run()

        assert list(tmp_path.iterdir()) == []

    def test_pipeline_continues_when_debug_write_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        monkeypatch.setenv("SAVE_DEBUG_OUTPUT", "true")
        # point to a path that can't be created (file exists where dir should be)
        bad_dir = tmp_path / "blocked.txt"
        bad_dir.write_text("I am a file, not a dir")
        monkeypatch.setenv("DEBUG_OUTPUT_DIR", str(bad_dir / "subdir"))
        from app.datapipeline.src.entrypoints.main import run

        with patch.multiple("app.datapipeline.src.entrypoints.main", **_build_mocks()):
            summary = run()

        assert summary["processed"] == 1  # pipeline completed despite write error


def _build_mocks(
    sources: list[PolicySource] | None = None,
    sync_changed: bool = True,
    questions: list[ExtractedQuestion] | None = None,
) -> dict:
    if sources is None:
        sources = [_SOURCE]
    if questions is None:
        questions = _QUESTIONS

    conn = MagicMock()
    sp = MagicMock()
    sp.read_page_content.return_value = ("Policy content", _TS)
    extractor = MagicMock()
    extractor.extract.return_value = (
        questions,
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "estimated_cost_usd": 0.0,
        },
    )

    return {
        "_get_db_connection": MagicMock(return_value=conn),
        "_build_sharepoint_client": MagicMock(return_value=sp),
        "_build_extractor": MagicMock(return_value=extractor),
        "fetch_all_policy_sources": MagicMock(return_value=sources),
        "load_local_policy_sources": MagicMock(return_value=sources),
        "get_sync_record": MagicMock(return_value=None),
        "is_changed": MagicMock(return_value=sync_changed),
        "insert_policy_document": MagicMock(return_value="doc-uuid"),
        "delete_questions_for_doc": MagicMock(return_value=0),
        "delete_policy_document_by_url": MagicMock(return_value=0),
        "insert_questions": MagicMock(return_value=len(questions)),
        "upsert_sync_record": MagicMock(),
    }
