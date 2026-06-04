import asyncpg
from fastapi import Depends, HTTPException, Request, status

from config import config
from utils.messages import messages
from repositories.cost_usage_repository import CostUsageRepository
from repositories.document_repository import DocumentRepository
from repositories.policy_document_repository import PolicyDocumentRepository
from repositories.user_repository import UserRepository
from services.access_code_service import AccessCodeService
from services.cost_usage_service import CostUsageService
from services.orchestrator_service import OrchestratorService
from services.policy_document_service import PolicyDocumentService
from services.s3_service import S3Service
from services.sqs_service import SQSService
from services.ssm_service import SsmService
from services.upload_service import UploadService
from utils.app_context import AppContext
from utils.auth import AuthService
from utils.logger import get_logger
from utils.postgres import get_db_pool

logger = get_logger(__name__)


def get_app_context() -> AppContext:
    return AppContext()


def get_document_repository(
    pool: asyncpg.Pool = Depends(get_db_pool),
    context: AppContext = Depends(get_app_context),
) -> DocumentRepository:
    return DocumentRepository(pool, context)


def get_user_repository(
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> UserRepository:
    return UserRepository(pool)


async def verify_auth(
    request: Request, user_repo: UserRepository = Depends(get_user_repository)
) -> dict:
    auth_header = request.headers.get("Authorization", "")
    sso_token = None
    if auth_header.lower().startswith("bearer "):
        sso_token = auth_header[7:].strip()

    claimed_user_id = request.headers.get(config.auth.user_id_header)
    if not claimed_user_id:
        logger.warning(
            "Authentication failed: Missing %s header", config.auth.user_id_header
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages.MISSING_USER_ID_HEADER.format(
                header=config.auth.user_id_header
            ),
        )

    token = AuthService.authorise_user(sso_token)
    verified_user_id = AuthService.get_user_id(token)

    if claimed_user_id != verified_user_id:
        logger.error(
            "Identity mismatch: header=%s token=%s", claimed_user_id, verified_user_id
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages.AUTH_IDENTITY_MISMATCH,
        )

    # Verify user exists in database
    user = await user_repo.get_user_by_id(verified_user_id)
    if user is None:
        logger.warning("Unauthorized: User %s not found in database", verified_user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages.AUTH_USER_NOT_FOUND,
        )

    return {"user_id": verified_user_id}


def get_cost_usage_repository(
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> CostUsageRepository:
    return CostUsageRepository(pool)


def get_cost_usage_service(
    repo: CostUsageRepository = Depends(get_cost_usage_repository),
) -> CostUsageService:
    return CostUsageService(repo)


def get_policy_document_repository(
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> PolicyDocumentRepository:
    return PolicyDocumentRepository(pool)


def get_policy_document_service(
    repo: PolicyDocumentRepository = Depends(get_policy_document_repository),
) -> PolicyDocumentService:
    return PolicyDocumentService(repo)


def get_s3_service() -> S3Service:
    return S3Service()


def get_sqs_service() -> SQSService:
    return SQSService()


def get_orchestrator_service() -> OrchestratorService:
    return OrchestratorService()


def get_upload_service(
    repo: DocumentRepository = Depends(get_document_repository),
    s3_service: S3Service = Depends(get_s3_service),
    context: AppContext = Depends(get_app_context),
    orchestrator_service: OrchestratorService = Depends(get_orchestrator_service),
) -> UploadService:
    return UploadService(repo, s3_service, context, orchestrator_service)


def get_access_code_service() -> AccessCodeService:
    # Stateless - SSM is hit fresh per call. Caching of the granted state
    # lives in the frontend session (`accessGranted` flag).
    return AccessCodeService(SsmService())
