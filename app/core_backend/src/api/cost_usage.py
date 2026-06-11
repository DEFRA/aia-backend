from fastapi import APIRouter, Depends, HTTPException, Query, status

from utils.dependencies import get_cost_usage_service, verify_auth
from utils.messages import messages
from models.cost_usage_record import CostUsageDocument, CostUsageResponse
from services.cost_usage_service import CostUsageService
from utils.logger import get_logger

router = APIRouter(prefix="/cost-usage", tags=["cost-usage"])
logger = get_logger(__name__)

_MAX_PAGE_LIMIT = 100


def _sanitize(value: str) -> str:
    """Strip newline/carriage-return characters to prevent log injection."""
    return str(value).replace("\n", "").replace("\r", "")


@router.get(
    "",
    response_model=CostUsageResponse,
    summary="Fetch paginated cost usage for the authenticated user",
)
async def fetch_cost_usage(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=_MAX_PAGE_LIMIT),
    auth: dict = Depends(verify_auth),
    service: CostUsageService = Depends(get_cost_usage_service),
) -> CostUsageResponse:
    user_id = auth["user_id"]
    response = await service.fetch_cost_usage(user_id, page=page, limit=limit)
    logger.info(
        "Cost usage userId=%s page=%d limit=%d total=%d",
        _sanitize(user_id),
        page,
        limit,
        response.pagination.total,
    )
    return response


@router.get(
    "/{document_id}",
    response_model=CostUsageDocument,
    summary="Fetch cost usage for a specific document",
)
async def fetch_cost_usage_by_document(
    document_id: str,
    auth: dict = Depends(verify_auth),
    service: CostUsageService = Depends(get_cost_usage_service),
) -> CostUsageDocument:
    user_id = auth["user_id"]
    document = await service.fetch_cost_usage_by_doc(document_id, user_id)
    if document is None:
        logger.warning(
            "Cost usage not found userId=%s documentId=%s", _sanitize(user_id), _sanitize(document_id)
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=messages.DOC_NOT_FOUND.format(doc_id=document_id),
        )
    logger.debug(
        "Cost usage by document userId=%s documentId=%s agents=%d",
        _sanitize(user_id),
        _sanitize(document_id),
        len(document.agents),
    )
    return document
