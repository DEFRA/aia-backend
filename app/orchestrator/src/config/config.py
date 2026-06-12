import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env only in local development. All other environments (ECS) receive
# secrets and config exclusively via the task definition at container start.
_env_path = Path(__file__).parent.parent.parent / ".env"
_ENV_FILE = _env_path if os.getenv("PYTHON_ENV") == "local" and _env_path.exists() else None

TEMPLATE_AGENTS: dict[str, list[str]] = {
    "SDA": ["security", "technical"],
    # "CHEDP": ["security", "data", "risk", "ea", "solution"],
}

# Pricing per million tokens (USD). Can be overridden via env var
# LLM_PRICING_USD_PER_MTOKENS as JSON.
DEFAULT_LLM_PRICING_USD_PER_MTOKENS: dict[str, dict[str, float]] = {
    # Bedrock model IDs
    "anthropic.claude-3-7-sonnet-20250219-v1:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 0.80, "output": 4.00},
    "anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # Anthropic direct model IDs
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-3-7-sonnet-20250219": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
}


class OrchestratorConfig(BaseModel):
    url: str = "http://localhost:8001"
    port: int = 8001
    agent_timeout_seconds: int = 480
    default_agent_type: str = "general"
    max_inline_bytes: int = 200_000  # stay well under SQS 256 KB limit


class DBConfig(BaseModel):
    uri: str


class AWSConfig(BaseModel):
    region: str
    access_key_id: Optional[str]
    secret_access_key: Optional[str]
    session_token: Optional[str] = None
    endpoint_url: Optional[str] = None


class S3Config(BaseModel):
    bucket_name: str


class SQSConfig(BaseModel):
    status_queue_url: str
    task_queue_url: str


class AppRunConfig(BaseModel):
    host: str = "127.0.0.1"
    env: str = "development"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Orchestrator
    orchestrator_url: str = Field("http://localhost:8001", alias="ORCHESTRATOR_URL")
    orchestrator_port: int = Field(8001, alias="ORCHESTRATOR_PORT")
    orchestrator_agent_timeout: int = Field(480, alias="AGENT_TIMEOUT_SECONDS")
    orchestrator_default_agent_type: str = Field(
        "general", alias="ORCHESTRATOR_DEFAULT_AGENT_TYPE"
    )
    orchestrator_max_inline_bytes: int = Field(
        200_000, alias="ORCHESTRATOR_MAX_INLINE_BYTES"
    )

    # Database
    db_host: str = Field("localhost", alias="DB_HOST")
    db_port: int = Field(5432, alias="DB_PORT")
    db_name: str = Field("assessments", alias="DB_NAME")
    db_user: str = Field("postgres", alias="DB_USER")
    db_password: str = Field("postgres", alias="DB_PASSWORD")

    # AWS
    aws_region: str = Field("us-east-1", alias="AWS_REGION")
    aws_default_region: Optional[str] = Field(None, alias="AWS_DEFAULT_REGION")
    aws_access_key_id: Optional[str] = Field(None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(None, alias="AWS_SECRET_ACCESS_KEY")
    aws_session_token: Optional[str] = Field(None, alias="AWS_SESSION_TOKEN")
    aws_endpoint_url: Optional[str] = Field(None, alias="AWS_ENDPOINT_URL")

    # SQS — supplied by environment (.env locally, ECS task definition in prod).
    status_queue_url: Optional[str] = Field(None, alias="STATUS_QUEUE_URL")
    task_queue_url: Optional[str] = Field(None, alias="TASK_QUEUE_URL")

    # S3
    documents_bucket: str = Field("documents", alias="S3_BUCKET_NAME")

    # Environment
    env: str = Field("development", alias="PYTHON_ENV")

    # LLM Pricing
    llm_pricing_usd_per_mtokens: dict[str, dict[str, float]] = Field(
        default_factory=lambda: DEFAULT_LLM_PRICING_USD_PER_MTOKENS.copy(),
        alias="LLM_PRICING_USD_PER_MTOKENS",
    )

    @property
    def db_connection_string(self) -> str:
        """Construct PostgreSQL connection string."""
        password = quote_plus(self.db_password) if self.db_password else ""
        user_part = f"{self.db_user}:{password}@" if self.db_user else ""
        return f"postgresql://{user_part}{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def db(self) -> "DBConfig":
        """Return database config object."""
        return DBConfig(uri=self.db_connection_string)

    @property
    def orchestrator(self) -> OrchestratorConfig:
        return OrchestratorConfig(
            url=self.orchestrator_url,
            port=self.orchestrator_port,
            agent_timeout_seconds=self.orchestrator_agent_timeout,
            default_agent_type=self.orchestrator_default_agent_type,
            max_inline_bytes=self.orchestrator_max_inline_bytes,
        )

    @property
    def aws(self) -> AWSConfig:
        # Only include credentials in development; production uses IAM role-based access
        is_production = self.env.lower() == "production"
        access_key = None if is_production else self.aws_access_key_id
        secret_key = None if is_production else self.aws_secret_access_key
        session_token = None if is_production else self.aws_session_token
        endpoint = self.aws_endpoint_url
        return AWSConfig(
            region=self.aws_default_region or self.aws_region,
            access_key_id=access_key,
            secret_access_key=secret_key,
            session_token=session_token,
            endpoint_url=endpoint,
        )

    @property
    def s3(self) -> S3Config:
        return S3Config(bucket_name=self.documents_bucket)

    @property
    def sqs(self) -> SQSConfig:
        return SQSConfig(
            status_queue_url=self.status_queue_url, task_queue_url=self.task_queue_url
        )

    @property
    def app(self) -> AppRunConfig:
        return AppRunConfig(host="127.0.0.1", env=self.env)

    @property
    def templates(self) -> dict[str, list[str]]:
        return TEMPLATE_AGENTS

    def get_agent_types(self, template_type: str) -> list[str]:
        """Return the agent types for a template. Falls back to default_agent_type when unknown."""
        return self.templates.get(template_type, [self.orchestrator.default_agent_type])


config = AppConfig()
