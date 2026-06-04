from contextlib import asynccontextmanager

import aiobotocore.session

from config import config
from utils.logger import get_logger

logger = get_logger(__name__)


class SsmService:
    @asynccontextmanager
    async def _get_client(self):
        session = aiobotocore.session.get_session()
        client_kwargs: dict = {
            "service_name": "ssm",
            "region_name": config.aws.region,
        }

        if config.env.lower() == "production":
            logger.debug("SSM client: production - using IAM role credentials")
        else:
            logger.debug(
                "SSM client: %s - using static credentials from env", config.env
            )
            if config.aws.access_key_id and config.aws.secret_access_key:
                client_kwargs["aws_access_key_id"] = config.aws.access_key_id
                client_kwargs["aws_secret_access_key"] = config.aws.secret_access_key
            if config.aws.session_token:
                client_kwargs["aws_session_token"] = config.aws.session_token

        if config.aws.endpoint_url:
            client_kwargs["endpoint_url"] = config.aws.endpoint_url

        async with session.create_client(**client_kwargs) as client:
            yield client

    async def get_parameter(self, name: str, with_decryption: bool = True) -> str:
        logger.debug("Fetching SSM parameter: %s", name)
        async with self._get_client() as client:
            response = await client.get_parameter(
                Name=name, WithDecryption=with_decryption
            )
            return response["Parameter"]["Value"]
