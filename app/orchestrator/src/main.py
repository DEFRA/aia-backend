import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from typing import Any

from pydantic import ValidationError
import uvicorn
from fastapi import BackgroundTasks, FastAPI

from .config import config  # noqa: E402
from .schemas.contracts import AgentResult  # noqa: E402
from .schemas.orchestrate_request import OrchestrateRequest  # noqa: E402
from .utils.document_parser import _parse_bytes  # noqa: E402
from .config.pipeline_config import PipelineConfig  # noqa: E402

from .utils.enums import DocumentStatus  # noqa: E402
from .schemas.status_message import StatusMessage  # noqa: E402
from .schemas.task_message import TaskMessage  # noqa: E402
from .session.store import SessionStore  # noqa: E402
from .reporting.report_generator import MarkdownReportGenerator  # noqa: E402
from .repositories.document_repository import DocumentRepository  # noqa: E402
from .repositories.cost_usage_repository import CostUsageRepository  # noqa: E402
from .services.s3_service import S3Service  # noqa: E402
from .services.sqs_service import SQSService  # noqa: E402
from .utils.app_context import AppContext  # noqa: E402
from .utils.logger import get_logger  # noqa: E402
from .utils.postgres import close_postgres_pool, get_postgres_pool, init_db  # noqa: E402

logger = get_logger("app.orchestrator")

# References: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html
_MAX_SQS_MESSAGE_BYTES = 1024 * 1024
_POSTGRES_INT_MAX = 2_147_483_647
_MAX_PRIORITY_ACTIONS = int(os.getenv("MAX_PRIORITY_ACTIONS", "10"))

_session_store = SessionStore()
_summary_generator = MarkdownReportGenerator()
_poller_task: asyncio.Task | None = None


class NonRetriableStatusMessageError(ValueError):
    """Raised when a status payload is malformed or violates invariants."""


def _known_agent_types() -> set[str]:
    known = {config.orchestrator.default_agent_type}
    for agent_types in config.templates.values():
        known.update(agent_types)
    return known


def _parse_status_message(body: str) -> StatusMessage:
    """Parse and validate a raw status queue payload.

    Raises NonRetriableStatusMessageError for malformed or poison messages so they can
    be deleted immediately instead of retried forever.
    """
    if len(body.encode("utf-8")) > _MAX_SQS_MESSAGE_BYTES:
        raise NonRetriableStatusMessageError("Status message exceeds SQS payload limit")

    try:
        status_msg = StatusMessage.model_validate_json(body)
    except ValidationError as exc:
        raise NonRetriableStatusMessageError(
            "Status payload validation failed"
        ) from exc

    expected_task_id = f"{status_msg.document_id}_{status_msg.agent_type}"
    if status_msg.task_id != expected_task_id:
        raise NonRetriableStatusMessageError(
            "task_id must match {document_id}_{agent_type}"
        )

    if status_msg.agent_type not in _known_agent_types():
        raise NonRetriableStatusMessageError("Unknown agent_type in status payload")

    if (
        status_msg.input_tokens is not None
        and status_msg.input_tokens > _POSTGRES_INT_MAX
    ):
        raise NonRetriableStatusMessageError("input_tokens exceeds DB integer limit")

    if (
        status_msg.output_tokens is not None
        and status_msg.output_tokens > _POSTGRES_INT_MAX
    ):
        raise NonRetriableStatusMessageError("output_tokens exceeds DB integer limit")

    return status_msg


def _pricing_map() -> dict[str, dict[str, float]]:
    return config.llm_pricing_usd_per_mtokens


def _calculate_total_cost_usd(
    model_id: str | None, input_tokens: int, output_tokens: int
) -> float:
    if not model_id:
        return 0.0
    rates = _pricing_map().get(model_id)
    if rates is None or "input" not in rates or "output" not in rates:
        return 0.0
    return round(
        (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000,
        6,
    )


async def _persist_status_tokens(
    status_msg: StatusMessage,
    cost_usage_repo: CostUsageRepository | None,
) -> None:
    """Persist token usage for one status message when token data is present."""
    if cost_usage_repo is None:
        logger.warning(
            "[TOKEN] Cost usage persistence skipped — DB unavailable task_id=%s doc_id=%s",
            status_msg.task_id,
            status_msg.document_id,
        )
        return

    if status_msg.input_tokens is None and status_msg.output_tokens is None:
        logger.info(
            "[TOKEN] No token payload to persist task_id=%s doc_id=%s agent_type=%s",
            status_msg.task_id,
            status_msg.document_id,
            status_msg.agent_type,
        )
        return

    raw_input_tokens = int(status_msg.input_tokens or 0)
    raw_output_tokens = int(status_msg.output_tokens or 0)
    if raw_input_tokens < 0 or raw_output_tokens < 0:
        logger.warning(
            "[TOKEN] Negative token values detected task_id=%s doc_id=%s input_tokens=%d output_tokens=%d",
            status_msg.task_id,
            status_msg.document_id,
            raw_input_tokens,
            raw_output_tokens,
        )

    input_tokens = max(raw_input_tokens, 0)
    output_tokens = max(raw_output_tokens, 0)

    total_cost_usd = _calculate_total_cost_usd(
        status_msg.model_id,
        input_tokens,
        output_tokens,
    )
    if status_msg.model_id and status_msg.model_id not in _pricing_map():
        logger.warning(
            "[TOKEN] No pricing configured for model_id=%s task_id=%s doc_id=%s; defaulting total_cost_usd=0.0",
            status_msg.model_id,
            status_msg.task_id,
            status_msg.document_id,
        )

    try:
        await cost_usage_repo.upsert_cost_usage(
            doc_id=status_msg.document_id,
            agent_name=status_msg.agent_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost_usd=total_cost_usd,
        )
        logger.info(
            "[TOKEN] Persisted cost usage task_id=%s doc_id=%s agent_type=%s model_id=%s input_tokens=%d output_tokens=%d total_cost_usd=%.6f",
            status_msg.task_id,
            status_msg.document_id,
            status_msg.agent_type,
            status_msg.model_id,
            input_tokens,
            output_tokens,
            total_cost_usd,
        )
    except Exception as exc:
        logger.error(
            "[TOKEN] Failed to persist cost usage task_id=%s doc_id=%s agent_type=%s: %s",
            status_msg.task_id,
            status_msg.document_id,
            status_msg.agent_type,
            exc,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _poller_task
    if config.db.uri:
        await init_db()
        logger.info("PostgreSQL initialised")
    _poller_task = asyncio.create_task(_status_queue_poller())
    logger.info("Orchestrator started — status queue poller running")
    yield
    if _poller_task:
        _poller_task.cancel()
    await close_postgres_pool()
    logger.info("Orchestrator stopped")


app = FastAPI(title="AIA Orchestrator", version="1.0.0", lifespan=lifespan)


@app.post("/orchestrate", status_code=202)
async def orchestrate(
    request: OrchestrateRequest, background_tasks: BackgroundTasks
) -> dict:
    logger.info(
        "Orchestrate request received doc_id=%s s3_key=%s template_type=%s",
        request.document_id,
        request.s3_key,
        request.template_type,
    )
    background_tasks.add_task(
        _process_document,
        request.document_id,
        request.s3_key,
        request.template_type,
    )
    return {"status": "accepted"}


def _extract_text(file_bytes: bytes, s3_key: str) -> str:
    """Extract plain text from PDF, DOCX, or UTF-8 text files."""
    lower = s3_key.lower()
    if lower.endswith(".pdf") or lower.endswith(".docx"):
        chunks = _parse_bytes(file_bytes, s3_key, "")
        return "\n\n".join(c["text"] for c in chunks if c.get("text"))
    return file_bytes.decode("utf-8", errors="replace")


async def _process_document(doc_id: str, s3_key: str, template_type: str) -> None:
    pool = await get_postgres_pool()
    context = AppContext()
    repo = DocumentRepository(pool, context)
    s3 = S3Service()
    sqs = SQSService()

    try:
        await repo.update_status(doc_id, DocumentStatus.PROCESSING.value)

        file_bytes = await s3.download_file(s3_key)
        file_content = _extract_text(file_bytes, s3_key)

        agent_types = config.get_agent_types(template_type)
        inline_content = (
            file_content
            if len(file_content.encode()) <= config.orchestrator.max_inline_bytes
            else None
        )

        # Fan-out: one task per agent_type — Agent Service owns the per-doc fan-out.
        tasks: list[TaskMessage] = []
        for agent_type in agent_types:
            tasks.append(
                TaskMessage(
                    task_id=f"{doc_id}_{agent_type}",
                    document_id=doc_id,
                    agent_type=agent_type,
                    template_type=template_type,
                    file_content=inline_content,
                    s3_bucket=None
                    if inline_content is not None
                    else config.s3.bucket_name,
                    s3_key=None if inline_content is not None else s3_key,
                )
            )

        await asyncio.gather(*[sqs.send_task(t) for t in tasks])
        logger.info(
            "Dispatched %d task(s) for doc_id=%s template_type=%s agents=%s",
            len(tasks),
            doc_id,
            template_type,
            agent_types,
        )

        if not tasks:
            await repo.update_status(
                doc_id,
                DocumentStatus.ERROR.value,
                error_message=(
                    f"No policy documents found for template '{template_type}'. "
                    "Check that the template type is valid and policy docs are loaded."
                ),
            )
            logger.error(
                "Document failed — 0 tasks dispatched doc_id=%s template_type=%s",
                doc_id,
                template_type,
            )
            return

        expected_task_ids = {t.task_id for t in tasks}
        session = await _session_store.create(
            doc_id, template_type, s3_key, expected_task_ids
        )

        timed_out = False
        try:
            await asyncio.wait_for(
                session.completion_event.wait(),
                timeout=float(config.orchestrator.agent_timeout_seconds),
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "Agent timeout reached for doc_id=%s after %ds",
                doc_id,
                config.orchestrator.agent_timeout_seconds,
            )

        if session.replaced:
            logger.info(
                "Session for doc_id=%s was superseded by a newer trigger — exiting",
                doc_id,
            )
            return

        expected = set(session.expected_task_ids)
        collected = dict(session.collected_results)
        await _session_store.remove(doc_id)

        _pipeline_cfg = PipelineConfig(max_priority_actions=_MAX_PRIORITY_ACTIONS)

        # Group results by agent_type — each value is a list of AgentResult (or None on error).
        by_agent_type: dict[str, list[Any]] = {}
        for task_id, result in collected.items():
            # task_id format: "{doc_id}_{agent_type}" — rsplit limits to one split
            _doc_id, agent_type = task_id.rsplit("_", 1)
            by_agent_type.setdefault(agent_type, []).append(result)

        # s3_key = "{doc_id}_{original_filename}" — strip the doc_id prefix for display
        document_title = Path(s3_key).name.removeprefix(f"{doc_id}_")

        if not timed_out:
            result_md = _summary_generator.generate(
                results=by_agent_type,
                document_title=document_title,
                section_labels=_pipeline_cfg.section_labels,
                agent_type_order=_pipeline_cfg.agent_types,
                max_priority_actions=_pipeline_cfg.max_priority_actions,
            )
            await repo.update_status(
                doc_id, DocumentStatus.COMPLETE.value, result_md=result_md
            )
            logger.info("Document completed doc_id=%s", doc_id)
        elif not collected:
            await repo.update_status(
                doc_id,
                DocumentStatus.ERROR.value,
                error_message="No agent responses received within timeout.",
            )
            logger.error("Document failed (0 responses) doc_id=%s", doc_id)
        else:
            result_md = _summary_generator.generate(
                results=by_agent_type,
                document_title=document_title,
                section_labels=_pipeline_cfg.section_labels,
                agent_type_order=_pipeline_cfg.agent_types,
                max_priority_actions=_pipeline_cfg.max_priority_actions,
            )
            missing = expected - collected.keys()
            # Extract agent_type from task_id for the warning message.
            missing_types = ", ".join(t.split("_")[1] for t in missing)
            await repo.update_status(
                doc_id,
                DocumentStatus.PARTIAL_COMPLETE.value,
                result_md=result_md,
                error_message=f"Agents that did not respond within the timeout: {missing_types}",
            )
            logger.warning(
                "Document partially completed doc_id=%s missing=%s",
                doc_id,
                missing_types,
            )

    except Exception as exc:
        logger.exception("Document processing failed doc_id=%s: %s", doc_id, exc)
        try:
            await repo.update_status(
                doc_id,
                DocumentStatus.ERROR.value,
                error_message=str(exc),
            )
        except Exception:
            logger.exception("Failed to write ERROR status for doc_id=%s", doc_id)


async def _status_queue_poller() -> None:
    """Continuously polls aia-status for agent results and routes them to the correct session."""
    sqs = SQSService()
    cost_usage_repo: CostUsageRepository | None = None
    if config.db.uri:
        pool = await get_postgres_pool()
        cost_usage_repo = CostUsageRepository(pool)
    queue_url = config.sqs.status_queue_url
    logger.info("Status queue poller started — polling %s", queue_url)

    while True:
        try:
            messages = await sqs.receive_messages(
                queue_url, max_messages=10, wait_seconds=20
            )
            for msg in messages:
                receipt = msg["receipt_handle"]
                body = msg.get("body", "")
                try:
                    status_msg = _parse_status_message(body)
                    logger.info(
                        "[TOKEN] Status message received task_id=%s doc_id=%s agent_type=%s model_id=%s inputTokens=%s outputTokens=%s",
                        status_msg.task_id,
                        status_msg.document_id,
                        status_msg.agent_type,
                        status_msg.model_id,
                        status_msg.input_tokens,
                        status_msg.output_tokens,
                    )
                    if status_msg.error:
                        agent_result = None
                    else:
                        agent_result = AgentResult.model_validate(status_msg.result)

                    # Pre-check session/task state so we can distinguish:
                    # - new valid result (persist tokens),
                    # - stale/unknown session (discard),
                    # - duplicate delivery (discard without double-counting).
                    session = _session_store.get(status_msg.document_id)
                    task_expected = (
                        session is not None
                        and status_msg.task_id in session.expected_task_ids
                    )
                    task_already_recorded = (
                        session is not None
                        and status_msg.task_id in session.collected_results
                    )

                    all_received = await _session_store.record_result(
                        status_msg.document_id,
                        status_msg.task_id,
                        agent_result,
                    )

                    if task_expected and not task_already_recorded:
                        await _persist_status_tokens(status_msg, cost_usage_repo)

                    await sqs.delete_message(queue_url, receipt)
                    if all_received is False:
                        # False means either unknown/expired session, unexpected
                        # task_id, or duplicate delivery; message is deleted to
                        # prevent queue build-up.
                        if task_already_recorded:
                            logger.warning(
                                "Discarded duplicate status message task_id=%s doc_id=%s",
                                status_msg.task_id,
                                status_msg.document_id,
                            )
                        else:
                            logger.warning(
                                "Discarded status message for unknown/expired session "
                                "task_id=%s doc_id=%s",
                                status_msg.task_id,
                                status_msg.document_id,
                            )
                    else:
                        logger.info(
                            "Result recorded task_id=%s all_received=%s",
                            status_msg.task_id,
                            all_received,
                        )
                except NonRetriableStatusMessageError as exc:
                    preview = body[:200].replace("\n", "\\n")
                    logger.warning(
                        "Discarding poison status message and deleting from queue: %s | body_preview=%s",
                        exc,
                        preview,
                    )
                    try:
                        await sqs.delete_message(queue_url, receipt)
                    except Exception:
                        logger.exception("Failed to delete poison status message")
                except Exception as exc:
                    logger.exception(
                        "Failed to process status message: %s — body=%s",
                        exc,
                        body,
                    )
        except asyncio.CancelledError:
            logger.info("Status queue poller stopped")
            return
        except Exception as exc:
            logger.exception("Status queue poller error: %s", exc)
            await asyncio.sleep(5)


def main() -> None:
    uvicorn.run(
        "app.orchestrator.src.main:app",
        host=config.app.host,
        port=config.orchestrator.port,
        reload=config.app.env.lower() == "local",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical("Fatal orchestrator error: %s", exc)
        sys.exit(1)
