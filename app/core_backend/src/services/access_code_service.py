import hashlib
import hmac

from config import config
from services.ssm_service import SsmService
from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_CODE_LENGTH = 36


class AccessCodeService:

    def __init__(self, ssm_service: SsmService) -> None:
        self._ssm = ssm_service

    async def is_valid(self, submitted: str) -> bool:
        if not submitted or len(submitted) > _MAX_CODE_LENGTH:
            return False

        if config.env.lower() in ("dev", "development"):
            valid_code = (config.auth.access_code or "").strip()
            valid_hash = (config.auth.access_code_hash or "").strip().lower()
        else:
            valid_code = (
                await self._ssm.get_parameter(
                    config.auth.ssm_access_code_param, with_decryption=True
                )
                or ""
            ).strip()
            valid_hash = (
                (
                    await self._ssm.get_parameter(
                        config.auth.ssm_access_code_hash_param, with_decryption=True
                    )
                    or ""
                )
                .strip()
                .lower()
            )

        if not valid_code or not valid_hash:
            return False

        if not hmac.compare_digest(submitted, valid_code):
            return False
        computed = hashlib.sha256(submitted.encode("utf-8")).hexdigest()
        return hmac.compare_digest(computed, valid_hash)
