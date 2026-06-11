import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils.enums import LogLevel

# Load .env only in local development. All other environments (ECS) receive
# secrets and config exclusively via the task definition at container start.
_env_path = Path(__file__).parent / ".env"
_ENV_FILE = _env_path if os.getenv("PYTHON_ENV") == "local" and _env_path.exists() else None

# ---------------------------------------------------------------------------
# Template → agent-type mapping.
# Add a new template type here to define which specialist agents run for it.
# ---------------------------------------------------------------------------

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


class AWSConfig(BaseModel):
    region: str = "eu-west-2"
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    endpoint_url: Optional[str] = None


class S3Config(BaseModel):
    bucket_name: str = "docsupload"
    upload_prefix: str = (
        ""  # e.g. "uploaded_docs" → keys land at {prefix}/{doc_id}_{filename}
    )


class SQSConfig(BaseModel):
    task_queue_url: str = "http://localhost:4566/000000000000/aia-tasks"
    status_queue_url: str = "http://localhost:4566/000000000000/aia-status"


class DBConfig(BaseModel):
    uri: Optional[str] = None


class AuthConfig(BaseModel):
    jwt_secret: str = "test_secret"
    user_id_header: str = "x-user-id"
    ssm_access_code_param: str = "/poc/ldn/webops/aia/access_code"
    ssm_access_code_hash_param: str = "/poc/ldn/webops/aia/access_code_hash"
    access_code: Optional[str] = None
    access_code_hash: Optional[str] = None


class OrchestratorConfig(BaseModel):
    url: str = "http://localhost:8001"
    port: int = 8001
    agent_timeout_seconds: int = 480
    default_agent_type: str = "general"
    max_inline_bytes: int = 200_000  # stay well under SQS 256 KB limit


class AppSettings(BaseModel):
    env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8086
    log_config: Optional[str] = None
    log_level: LogLevel = LogLevel.INFO
    http_proxy: Optional[HttpUrl] = None
    tracing_header: str = "x-cdp-request-id"
    worker_stuck_task_timeout_minutes: int = 120


class AppConfig(BaseSettings):
    # Application
    env: str = Field("dev", alias="PYTHON_ENV")
    host: str = Field("127.0.0.1", alias="HOST")
    port: int = Field(8086, alias="PORT")
    log_config: Optional[str] = Field(None, alias="LOG_CONFIG")
    log_level: LogLevel = Field(LogLevel.INFO, alias="LOG_LEVEL")
    http_proxy: Optional[HttpUrl] = Field(None, alias="HTTP_PROXY")
    tracing_header: str = Field("x-cdp-request-id", alias="TRACING_HEADER")
    worker_stuck_task_timeout_minutes: int = Field(
        120, alias="WORKER_STUCK_TASK_TIMEOUT_MINUTES"
    )

    # AWS
    aws_region: str = Field("eu-west-2", alias="AWS_DEFAULT_REGION")
    aws_access_key: str = Field("test", alias="AWS_ACCESS_KEY_ID")
    aws_secret_key: str = Field("test", alias="AWS_SECRET_ACCESS_KEY")
    aws_session_token: Optional[str] = Field(None, alias="AWS_SESSION_TOKEN")
    aws_endpoint: Optional[str] = Field(None, alias="AWS_ENDPOINT_URL")

    # S3
    s3_bucket: str = Field("docsupload", alias="S3_BUCKET_NAME")
    s3_upload_prefix: str = Field("", alias="S3_UPLOAD_PREFIX")

    # SQS
    sqs_task_url: str = Field(
        "http://localhost:4566/000000000000/aia-tasks", alias="TASK_QUEUE_URL"
    )
    sqs_status_url: str = Field(
        "http://localhost:4566/000000000000/aia-status", alias="STATUS_QUEUE_URL"
    )

    # DB
    db_uri: Optional[str] = Field(None, alias="POSTGRES_URI")
    db_host: Optional[str] = Field(None, alias="DB_HOST")
    db_port: int = Field(5432, alias="DB_PORT")
    db_name: Optional[str] = Field(None, alias="DB_NAME")
    db_user: Optional[str] = Field(None, alias="DB_USER")
    db_password: Optional[str] = Field(None, alias="DB_PASSWORD")

    # Auth
    jwt_secret: str = Field("test_secret", alias="JWT_SECRET")
    user_id_header: str = Field("x-user-id", alias="USER_ID_HEADER")
    ssm_access_code_param: str = Field(
        "/poc/ldn/webops/aia/access_code", alias="SSM_ACCESS_CODE_PARAM"
    )
    ssm_access_code_hash_param: str = Field(
        "/poc/ldn/webops/aia/access_code_hash", alias="SSM_ACCESS_CODE_HASH_PARAM"
    )
    access_code: Optional[str] = Field(None, alias="ACCESS_CODE")
    access_code_hash: Optional[str] = Field(None, alias="ACCESS_CODE_HASH")

    # Orchestrator
    orchestrator_url: str = Field("http://localhost:8001", alias="ORCHESTRATOR_URL")
    orchestrator_port: int = Field(8001, alias="ORCHESTRATOR_PORT")
    orchestrator_agent_timeout: int = Field(480, alias="AGENT_TIMEOUT_SECONDS")
    orchestrator_default_agent_type: str = Field(
        "general", alias="ORCHESTRATOR_DEFAULT_AGENT_TYPE"
    )
    max_file_upload: int = Field(50, alias="MAX_FILE_UPLOAD")
    allowed_extensions_raw: str = Field(".docx", alias="ALLOWED_FILE_EXTENSIONS")
    llm_pricing_usd_per_mtokens: dict[str, dict[str, float]] = Field(
        default_factory=lambda: DEFAULT_LLM_PRICING_USD_PER_MTOKENS.copy(),
        alias="LLM_PRICING_USD_PER_MTOKENS",
    )

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def allowed_file_extensions(self) -> list[str]:
        return [
            ext.strip().lower()
            for ext in self.allowed_extensions_raw.split(",")
            if ext.strip()
        ]

    @property
    def app(self) -> AppSettings:
        return AppSettings(
            env=self.env,
            host=self.host,
            port=self.port,
            log_config=self.log_config,
            log_level=self.log_level,
            http_proxy=self.http_proxy,
            tracing_header=self.tracing_header,
            worker_stuck_task_timeout_minutes=self.worker_stuck_task_timeout_minutes,
        )

    @property
    def aws(self) -> AWSConfig:
        use_static_credentials = self.env.lower() != "production"
        endpoint = self.aws_endpoint
        if endpoint is None and self.env.lower() in ("dev", "development"):
            endpoint = "http://localhost:4566"
        return AWSConfig(
            region=self.aws_region,
            access_key_id=self.aws_access_key if use_static_credentials else None,
            secret_access_key=self.aws_secret_key if use_static_credentials else None,
            session_token=self.aws_session_token if use_static_credentials else None,
            endpoint_url=endpoint,
        )

    @property
    def s3(self) -> S3Config:
        return S3Config(bucket_name=self.s3_bucket, upload_prefix=self.s3_upload_prefix)

    @property
    def sqs(self) -> SQSConfig:
        return SQSConfig(
            task_queue_url=self.sqs_task_url,
            status_queue_url=self.sqs_status_url,
        )

    @property
    def db(self) -> DBConfig:
        if self.db_uri:
            return DBConfig(uri=self.db_uri)

        if all([self.db_host, self.db_name, self.db_user, self.db_password]):
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            uri = f"postgresql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
            return DBConfig(uri=uri)

        return DBConfig(uri=None)

    @property
    def auth(self) -> AuthConfig:
        return AuthConfig(
            jwt_secret=self.jwt_secret,
            user_id_header=self.user_id_header,
            ssm_access_code_param=self.ssm_access_code_param,
            ssm_access_code_hash_param=self.ssm_access_code_hash_param,
            access_code=self.access_code,
            access_code_hash=self.access_code_hash,
        )

    @property
    def orchestrator(self) -> OrchestratorConfig:
        return OrchestratorConfig(
            url=self.orchestrator_url,
            port=self.orchestrator_port,
            agent_timeout_seconds=self.orchestrator_agent_timeout,
            default_agent_type=self.orchestrator_default_agent_type,
        )

    @property
    def templates(self) -> dict[str, list[str]]:
        return TEMPLATE_AGENTS

    def get_agent_types(self, template_type: str) -> list[str]:
        """Return the agent types for a template. Falls back to default_agent_type when unknown."""
        return self.templates.get(template_type, [self.orchestrator.default_agent_type])


config = AppConfig()
