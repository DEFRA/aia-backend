from fastapi import APIRouter, Depends, HTTPException, status

from models.access_code import AccessCodeRequest, AccessCodeResponse
from services.access_code_service import AccessCodeService
from utils.dependencies import get_access_code_service
from utils.logger import get_logger
from utils.messages import messages

router = APIRouter(prefix="/access-code", tags=["access-code"])
logger = get_logger(__name__)


@router.post(
    "/validate",
    response_model=AccessCodeResponse,
    status_code=status.HTTP_200_OK,
    summary="Validate the access code against SSM-stored values. Returns 403 on any failure.",
)
async def validate_access_code(
    payload: AccessCodeRequest,
    service: AccessCodeService = Depends(get_access_code_service),
) -> AccessCodeResponse:
    # CONTRACT: this endpoint must only be invoked in response to an explicit
    # user action (submitting the access-code form). Do NOT wire it into
    # background jobs, session keep-alives, or auth middleware - the frontend
    # already caches the grant in the user's session (`accessGranted` flag,
    # ~4h cookie / 1h idle). Each call here triggers a fresh SSM lookup.
    try:
        is_valid = await service.is_valid(payload.accessCode)
    except Exception as exc:
        # Hide infra failures behind the same 403 so the response surface is
        # uniform - operators see the detail in the server logs.
        logger.exception("Access-code validation aborted: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages.ACCESS_CODE_INVALID,
        ) from exc

    if not is_valid:
        logger.warning("Access code rejected")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages.ACCESS_CODE_INVALID,
        )

    logger.info("Access code accepted")
    return AccessCodeResponse(valid=True)
