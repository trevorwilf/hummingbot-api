import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlparse

import logfire
from dotenv import load_dotenv

# Apply the patch before importing hummingbot components
from hummingbot.client.config import config_helpers

# Load environment variables early
load_dotenv()

VERSION = "1.0.1"


# Monkey patch save_to_yml to prevent writes to library directory
def patched_save_to_yml(yml_path, cm):
    """Patched version of save_to_yml that prevents writes to library directory"""
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Skipping config write to {yml_path} (patched for API mode)")
    # Do nothing - this prevents the original function from trying to write to the library directory


config_helpers.save_to_yml = patched_save_to_yml

from fastapi import Depends, FastAPI, HTTPException, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from hummingbot.client.config.client_config_map import GatewayConfigMap  # noqa: E402
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger  # noqa: E402
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient  # noqa: E402
from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES, RateOracle  # noqa: E402

from config import settings  # noqa: E402
from database import AsyncDatabaseManager  # noqa: E402
from routers import (  # noqa: E402
    accounts,
    archived_bots,
    backtesting,
    bot_orchestration,
    connectors,
    controllers,
    docker,
    executors,
    gateway,
    gateway_clmm,
    gateway_swap,
    market_data,
    portfolio,
    rate_oracle,
    scripts,
    trading,
)
from services.accounts_service import AccountsService  # noqa: E402
from services.bots_orchestrator import BotsOrchestrator  # noqa: E402
from services.docker_service import DockerService  # noqa: E402
from services.executor_service import ExecutorService  # noqa: E402
from services.gateway_service import GatewayService  # noqa: E402
from services.market_data_service import MarketDataService  # noqa: E402
from services.trading_service import TradingService  # noqa: E402
from services.unified_connector_service import UnifiedConnectorService  # noqa: E402
from utils.bot_archiver import BotArchiver  # noqa: E402
from utils.security import BackendAPISecurity  # noqa: E402

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Enable info logging for MQTT manager
logging.getLogger('services.mqtt_manager').setLevel(logging.INFO)

# Get settings from Pydantic Settings
username = settings.security.username
password = settings.security.password
debug_mode = settings.security.debug_mode

if debug_mode:
    logging.warning("=" * 60)
    logging.warning("  WARNING: DEBUG MODE IS ENABLED — AUTH IS DISABLED")
    logging.warning("  All API endpoints are accessible without credentials.")
    logging.warning("  DO NOT run this in production!")
    logging.warning("=" * 60)

# Security setup
security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for the FastAPI application.
    Handles startup and shutdown events.
    """
    # Ensure password verification file exists
    if BackendAPISecurity.new_password_required():
        # Create secrets manager with CONFIG_PASSWORD
        secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
        BackendAPISecurity.store_password_verification(secrets_manager)
        logging.info("Created password verification file for master_account")

    # =========================================================================
    # 1. Infrastructure Setup
    # =========================================================================

    # Initialize GatewayHttpClient singleton
    parsed_gateway_url = urlparse(settings.gateway.url)
    gateway_config = GatewayConfigMap(
        gateway_api_host=parsed_gateway_url.hostname or "localhost",
        gateway_api_port=str(parsed_gateway_url.port or 15888),
        gateway_use_ssl=parsed_gateway_url.scheme == "https"
    )
    GatewayHttpClient.get_instance(gateway_config)
    logging.info(f"Initialized GatewayHttpClient with URL: {settings.gateway.url}")

    # Initialize secrets manager and database
    secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
    db_manager = AsyncDatabaseManager(settings.database.url)
    await db_manager.create_tables()
    logging.info("Database initialized")

    # Read rate oracle configuration from conf_client.yml
    from utils.file_system import FileSystemUtil
    fs_util = FileSystemUtil()

    try:
        conf_client_path = "credentials/master_account/conf_client.yml"
        config_data = fs_util.read_yaml_file(conf_client_path)

        # Get rate_oracle_source configuration
        rate_oracle_source_data = config_data.get("rate_oracle_source", {})
        source_name = rate_oracle_source_data.get("name", "binance")

        # Get global_token configuration
        global_token_data = config_data.get("global_token", {})
        quote_token = global_token_data.get("global_token_name", "USDT")

        # Create rate source instance
        if source_name in RATE_ORACLE_SOURCES:
            rate_source = RATE_ORACLE_SOURCES[source_name]()
            logging.info(f"Configured RateOracle with source: {source_name}, quote_token: {quote_token}")
        else:
            logging.warning(f"Unknown rate oracle source '{source_name}', defaulting to binance")
            rate_source = RATE_ORACLE_SOURCES["binance"]()
            source_name = "binance"

        # Initialize RateOracle with configured source and quote token
        rate_oracle = RateOracle.get_instance()
        rate_oracle.source = rate_source
        rate_oracle.quote_token = quote_token

    except FileNotFoundError:
        logging.warning("conf_client.yml not found, using default RateOracle configuration (binance, USDT)")
        rate_oracle = RateOracle.get_instance()
    except Exception as e:
        logging.warning(f"Error reading conf_client.yml: {e}, using default RateOracle configuration")
        rate_oracle = RateOracle.get_instance()

    # =========================================================================
    # 2. UnifiedConnectorService - Single source of truth for all connectors
    # =========================================================================

    connector_service = UnifiedConnectorService(
        secrets_manager=secrets_manager,
        db_manager=db_manager
    )
    logging.info("UnifiedConnectorService initialized")

    # =========================================================================
    # 3. Services that depend on connector_service
    # =========================================================================

    # MarketDataService - candles, order books, prices
    market_data_service = MarketDataService(
        connector_service=connector_service,
        rate_oracle=rate_oracle,
        cleanup_interval=settings.market_data.cleanup_interval,
        feed_timeout=settings.market_data.feed_timeout
    )
    logging.info("MarketDataService initialized")

    # TradingService - order placement, positions, trading interfaces
    trading_service = TradingService(
        connector_service=connector_service,
        market_data_service=market_data_service
    )
    logging.info("TradingService initialized")

    # AccountsService - account management, balances, portfolio (simplified)
    accounts_service = AccountsService(
        account_update_interval=settings.app.account_update_interval,
        gateway_url=settings.gateway.url
    )
    # Inject services into AccountsService
    accounts_service._connector_service = connector_service
    accounts_service._market_data_service = market_data_service
    accounts_service._trading_service = trading_service
    logging.info("AccountsService initialized")

    # =========================================================================
    # 4. ExecutorService - depends on TradingService (NO circular dependency)
    # =========================================================================

    executor_service = ExecutorService(
        trading_service=trading_service,
        db_manager=db_manager,
        default_account="master_account",
        update_interval=1.0,
        max_retries=10
    )
    logging.info("ExecutorService initialized")

    # =========================================================================
    # 5. Other Services
    # =========================================================================

    bots_orchestrator = BotsOrchestrator(
        broker_host=settings.broker.host,
        broker_port=settings.broker.port,
        broker_username=settings.broker.username,
        broker_password=settings.broker.password
    )

    docker_service = DockerService()
    gateway_service = GatewayService()
    bot_archiver = BotArchiver(
        settings.aws.api_key,
        settings.aws.secret_key,
        settings.aws.s3_default_bucket_name
    )

    # =========================================================================
    # 6. Start services
    # =========================================================================

    # Initialize all trading connectors FIRST (before any service that might use them)
    # This ensures OrdersRecorder is properly attached before any concurrent access
    logging.info("Initializing all trading connectors...")
    await connector_service.initialize_all_trading_connectors()

    bots_orchestrator.start()
    market_data_service.start()
    await market_data_service.warmup_rate_oracle()
    executor_service.start()
    await executor_service.cleanup_orphaned_executors()
    await executor_service.recover_positions_from_db()
    accounts_service.start()

    # =========================================================================
    # 7. Store services in app state
    # =========================================================================

    app.state.db_manager = db_manager
    app.state.connector_service = connector_service
    app.state.market_data_service = market_data_service
    app.state.trading_service = trading_service
    app.state.accounts_service = accounts_service
    app.state.executor_service = executor_service
    app.state.bots_orchestrator = bots_orchestrator
    app.state.docker_service = docker_service
    app.state.gateway_service = gateway_service
    app.state.bot_archiver = bot_archiver

    logging.info("All services started successfully")

    yield

    # =========================================================================
    # Shutdown services
    # =========================================================================

    logging.info("Shutting down services...")

    bots_orchestrator.stop()
    await accounts_service.stop()
    await executor_service.stop()
    market_data_service.stop()
    await connector_service.stop_all()
    docker_service.cleanup()
    await db_manager.close()

    logging.info("All services stopped")


# Initialize FastAPI with metadata and lifespan
app = FastAPI(
    title="Hummingbot API",
    description="API for managing Hummingbot trading instances",
    version=VERSION,
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8501",    # Dashboard (Streamlit)
        "http://localhost:8501",
        "http://127.0.0.1:8080",    # Dozzle
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Rate Limiting Middleware
# ---------------------------------------------------------------------------
import time as _time
from collections import defaultdict as _defaultdict


class SimpleRateLimiter:
    """In-memory token-bucket rate limiter. Resets per minute."""

    def __init__(self, default_rpm: int = 120, trading_rpm: int = 30):
        self._default_rpm = default_rpm
        self._trading_rpm = trading_rpm
        self._requests: dict[str, list[float]] = _defaultdict(list)

    def check(self, key: str, limit: int = None) -> bool:
        now = _time.time()
        rpm = limit or self._default_rpm
        window = self._requests[key]
        # Prune entries older than 60 seconds
        self._requests[key] = [t for t in window if now - t < 60]
        if len(self._requests[key]) >= rpm:
            return False
        self._requests[key].append(now)
        return True


_rate_limiter = SimpleRateLimiter(default_rpm=120, trading_rpm=30)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    # Stricter limit for trading endpoints
    if path.startswith("/trading/place") or path.startswith("/trading/cancel"):
        limit = 30
    else:
        limit = 120

    if not _rate_limiter.check(f"{client_ip}:{path}", limit):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again in a minute."}
        )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler for validation errors to log detailed error messages.
    """
    # Build a readable error message from validation errors
    error_messages = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{loc}: {msg}")

    # Log the validation error with details
    logging.warning(
        f"Validation error on {request.method} {request.url.path}: {'; '.join(error_messages)}"
    )

    # Return standard FastAPI validation error response
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )


logfire.configure(send_to_logfire="if-token-present", environment=settings.app.logfire_environment,
                  service_name="hummingbot-api")
logfire.instrument_fastapi(app)


def auth_user(
        credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    """Authenticate user using HTTP Basic Auth"""
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = f"{username}".encode("utf8")
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = f"{password}".encode("utf8")
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    if not (is_correct_username and is_correct_password):
        if debug_mode:
            logging.warning(f"Auth bypassed in debug mode for user: {credentials.username}")
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Basic"},
            )
    return credentials.username


# Include all routers with authentication
app.include_router(docker.router, dependencies=[Depends(auth_user)])
app.include_router(gateway.router, dependencies=[Depends(auth_user)])
app.include_router(accounts.router, dependencies=[Depends(auth_user)])
app.include_router(connectors.router, dependencies=[Depends(auth_user)])
app.include_router(portfolio.router, dependencies=[Depends(auth_user)])
app.include_router(trading.router, dependencies=[Depends(auth_user)])
app.include_router(gateway_swap.router, dependencies=[Depends(auth_user)])
app.include_router(gateway_clmm.router, dependencies=[Depends(auth_user)])
app.include_router(bot_orchestration.router, dependencies=[Depends(auth_user)])
app.include_router(controllers.router, dependencies=[Depends(auth_user)])
app.include_router(scripts.router, dependencies=[Depends(auth_user)])
app.include_router(market_data.router, dependencies=[Depends(auth_user)])
app.include_router(rate_oracle.router, dependencies=[Depends(auth_user)])
app.include_router(backtesting.router, dependencies=[Depends(auth_user)])
app.include_router(archived_bots.router, dependencies=[Depends(auth_user)])
app.include_router(executors.router, dependencies=[Depends(auth_user)])


@app.get("/")
async def root():
    """API root endpoint returning basic information."""
    return {
        "name": "Hummingbot API",
        "version": VERSION,
        "status": "running",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration and monitoring.

    Returns component status for: API, database, MQTT broker.
    Does not require authentication.
    """
    from sqlalchemy import text
    from starlette.responses import JSONResponse

    health = {
        "status": "healthy",
        "components": {}
    }

    # Check database
    try:
        db_manager = app.state.db_manager
        async with db_manager.session() as session:
            await session.execute(text("SELECT 1"))
        health["components"]["database"] = {"status": "healthy"}
    except Exception as e:
        health["status"] = "degraded"
        health["components"]["database"] = {"status": "unhealthy", "error": str(e)}

    # Check MQTT broker
    try:
        orchestrator = app.state.bots_orchestrator
        if orchestrator and orchestrator.mqtt_manager:
            mqtt_connected = orchestrator.mqtt_manager.is_connected
            health["components"]["mqtt"] = {
                "status": "healthy" if mqtt_connected else "unhealthy"
            }
        else:
            health["components"]["mqtt"] = {"status": "not_initialized"}
    except Exception as e:
        health["components"]["mqtt"] = {"status": "unknown", "error": str(e)}

    # Check connector service
    try:
        connector_service = app.state.connector_service
        health["components"]["connector_service"] = {
            "status": "healthy" if connector_service else "not_initialized"
        }
    except Exception as e:
        health["components"]["connector_service"] = {"status": "unknown", "error": str(e)}

    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)
