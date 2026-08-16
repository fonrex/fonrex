import importlib
import logging
import os
import time
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from cache.service import CacheService
from cache.technical import RedisTechnicalCache
from concurrency import run_sync
from database.lifecycle import AsyncDatabaseResources
from database.monitoring import SqlAlchemyMonitoringRepository
from database.query import QueryService
from database.service import DatabaseService
from database.technical import SqlAlchemyTechnicalRepository
from documentation import get_api_documentation
from financials.router import router as financials_router
from financials.service import FinancialsAggregator
from historical.ingestion_service import HistoricalIngestionService
from monitoring.canary_monitor import CanaryMonitor
from monitoring.validation_layer import ValidationLayer
from news.news_service import NewsService
from realtime.connection_manager import ConnectionManager
from realtime.worker import RealtimePriceWorker
from routers.admin import router as admin_router
from routers.assets import router as assets_router
from routers.fundamentals import router as fundamentals_router
from routers.historical import router as historical_router
from routers.monitoring import router as monitoring_router
from routers.news import router as news_router
from routers.realtime import router as realtime_router
from routers.specialized import router as specialized_router
from routers.technical import router as technical_router
from routers.valuation import router as valuation_router
from valuation.dcf_service import DCFService

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Initialize and reliably release process-level application resources."""
    try:
        await startup_event(_app)
        yield
    finally:
        await shutdown_event(_app)


app = FastAPI(title="FonRex API", version="2.0.0", lifespan=app_lifespan)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(financials_router)
app.include_router(admin_router)
app.include_router(news_router)
app.include_router(valuation_router)
app.include_router(technical_router)
app.include_router(historical_router)
app.include_router(assets_router)
app.include_router(fundamentals_router)
app.include_router(specialized_router)
app.include_router(realtime_router)

app.include_router(monitoring_router)


@app.middleware("http")
async def usage_logging_middleware(request: Request, call_next):
    """Logs API calls without interrupting the user response."""
    start_time = time.perf_counter()
    status_code = 500
    response = None

    try:
        response = await call_next(request)
        status_code = response.status_code
    finally:
        service = None
        if getattr(request.app.state, "db_available", True) is not False:
            service = getattr(request.app.state, "db_service", None)

        if service:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            try:
                await run_sync(
                    service.log_usage,
                    endpoint=request.url.path,
                    method=request.method,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    api_key_id=request.headers.get("X-API-Key"),
                    provider_used=getattr(request.state, "provider_used", None),
                    cache_hit=getattr(request.state, "cache_hit", False),
                    cost_bucket=getattr(request.state, "cost_bucket", None),
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("User-Agent"),
                )
            except Exception as e:
                logger.warning(f"Usage log skipped: {e}")

    return response


# Redis configuration
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.environ.get("CACHE_TTL", 300))


def configure_application_state(application: FastAPI):
    """Configure provider definitions and empty runtime service slots."""
    providers = {}
    provider_specs = (
        ("ZoneBourse", "financials.providers.ZoneBourse_provider", "ZoneBourseProvider"),
        ("GoogleFinance", "financials.providers.GoogleFinance_provider", "GoogleFinanceProvider"),
        ("Boursorama", "financials.providers.boursorama_provider", "BoursoramaProvider"),
        ("Barrons", "financials.providers.Barrons_provider", "BarronsProvider"),
        (
            "wallStreetJournal",
            "financials.providers.wallStreetJournal_provider",
            "WallStreetJournalProvider",
        ),
        ("Marketwatch", "financials.providers.Marketwatch_provider", "MarketwatchProvider"),
        ("MorningStar", "financials.providers.MorningStar_provider", "MorningStarProvider"),
        ("Investing", "financials.providers.Investing_provider", "InvestingProvider"),
        ("Gurufocus", "financials.providers.Gurufocus_provider", "GurufocusProvider"),
        ("Fortuneo", "financials.providers.Fortuneo_provider", "FortuneoProvider"),
        ("BourseDirect", "financials.providers.BourseDirect_provider", "BourseDirectProvider"),
        ("Msn", "financials.providers.Msn_provider", "MsnProvider"),
        (
            "InvestirLesEchos",
            "financials.providers.InvestirLesEchos_provider",
            "InvestirLesEchosProvider",
        ),
        ("YahooFinance", "financials.providers.yfinance_provider", "YFinanceProvider"),
    )
    for name, module_name, class_name in provider_specs:
        try:
            provider_class = getattr(importlib.import_module(module_name), class_name)
            providers[name] = {"type": "async", "class": provider_class}
            logger.info("✅ Provider Async %s loaded", name)
        except (ImportError, AttributeError) as exc:
            logger.warning("⚠️ Provider Async %s unavailable: %s", name, exc)

    specialized_specs = (
        (
            "sec_edgar_provider",
            "SECEdgar",
            "financials.providers.sec_edgar",
            "SECEdgarProvider",
        ),
        (
            "justetf_provider",
            "JustETF",
            "financials.providers.justetf",
            "JustETFProvider",
        ),
        (
            "openfigi_provider",
            "OpenFIGI",
            "financials.providers.openfigi",
            "OpenFIGIProvider",
        ),
        (
            "index_provider",
            "IndexConstituents",
            "financials.providers.index_constituents",
            "IndexConstituentsProvider",
        ),
    )
    for state_name, label, module_name, class_name in specialized_specs:
        try:
            provider_class = getattr(importlib.import_module(module_name), class_name)
            setattr(application.state, state_name, provider_class())
            logger.info("✅ Provider %s loaded", label)
        except (ImportError, AttributeError) as exc:
            setattr(application.state, state_name, None)
            logger.warning("⚠️ Provider %s unavailable: %s", label, exc)

    try:
        index_module = importlib.import_module("financials.providers.index_constituents")
        application.state.index_name_enum = index_module.IndexName
    except (ImportError, AttributeError):
        application.state.index_name_enum = None

    application.state.providers_available = providers
    application.state.ws_manager = ConnectionManager()
    application.state.db_available = None
    for state_name in (
        "async_db_resources",
        "async_session_factory",
        "query_service",
        "redis_client",
        "db_service",
        "cache_service",
        "financials_service",
        "ingestion_service",
        "technical_service",
        "realtime_worker",
        "news_service",
        "dcf_service",
        "validation_layer",
        "canary_monitor",
        "canary_scheduler",
    ):
        setattr(application.state, state_name, None)


configure_application_state(app)


async def startup_event(application: FastAPI):
    """Build process resources and publish them through application state."""
    state = application.state
    state.async_db_resources = AsyncDatabaseResources.create()
    async_resources = state.async_db_resources

    state.query_service = QueryService(
        session_factory=async_resources.session_factory if async_resources else None,
        engine=async_resources.engine if async_resources else None,
    )
    state.db_service = await run_sync(DatabaseService)
    state.db_available = await run_sync(state.db_service.check_connection)
    if state.db_available:
        state.db_available = await run_sync(state.db_service.check_migrations)

    state.redis_client = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    state.cache_service = await run_sync(CacheService, redis_url=REDIS_URL, ttl=CACHE_TTL)
    state.financials_service = FinancialsAggregator(state.redis_client)
    state.ingestion_service = HistoricalIngestionService(
        state.db_service, state.query_service, state.redis_client
    )

    from technical.indicator_service import TechnicalIndicatorService

    state.technical_service = TechnicalIndicatorService(
        SqlAlchemyTechnicalRepository(state.db_service),
        RedisTechnicalCache(state.redis_client),
    )

    if async_resources:
        state.async_session_factory = async_resources.session_factory

    try:
        if async_resources:
            state.realtime_worker = RealtimePriceWorker(
                state.redis_client, async_resources.session_factory
            )
            await state.realtime_worker.start()
            logger.info("📡 RealtimePriceWorker started")
        else:
            logger.warning("⚠️ DATABASE_URL not set — RealtimePriceWorker disabled")
    except Exception as exc:
        state.realtime_worker = None
        logger.warning("⚠️ RealtimePriceWorker not started: %s", exc)

    try:
        if async_resources:
            state.news_service = NewsService(
                redis_client=state.redis_client,
                session_factory=async_resources.session_factory,
            )
            logger.info("📰 NewsService started")
        else:
            logger.warning("⚠️ DATABASE_URL not set — NewsService disabled")
    except Exception as exc:
        state.news_service = None
        logger.warning("⚠️ NewsService not started: %s", exc)

    try:
        state.dcf_service = DCFService(state.db_service, state.redis_client)
        logger.info("📈 DCFService started")
    except Exception as exc:
        state.dcf_service = None
        logger.warning("⚠️ DCFService not started: %s", exc)

    try:
        if async_resources:
            monitoring_repository = SqlAlchemyMonitoringRepository(async_resources.session_factory)
            state.validation_layer = ValidationLayer(monitoring_repository)
            state.canary_monitor = CanaryMonitor(monitoring_repository, state.redis_client)

            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler

                state.canary_scheduler = AsyncIOScheduler()
                canary_hour = int(os.environ.get("CANARY_RUN_HOUR", "6"))
                state.canary_scheduler.add_job(
                    state.canary_monitor.run_all,
                    "cron",
                    hour=canary_hour,
                    minute=0,
                    timezone="UTC",
                    misfire_grace_time=3600,
                    id="canary_daily",
                )
                state.canary_scheduler.start()
                logger.info("🩺 CanaryMonitor scheduled daily at %02d:00 UTC", canary_hour)
            except ImportError:
                logger.warning("⚠️ apscheduler not installed — canary scheduling disabled")

            logger.info("🩺 Provider Monitoring (ValidationLayer + CanaryMonitor) started")
        else:
            logger.warning("⚠️ DATABASE_URL not set — Provider Monitoring disabled")
    except Exception as exc:
        state.validation_layer = None
        state.canary_monitor = None
        state.canary_scheduler = None
        logger.warning("⚠️ Provider Monitoring not started: %s", exc)

    logger.info("🚀 FonRex API (FastAPI) started")


async def shutdown_event(application: FastAPI):
    """Close application resources without relying on module-level state."""
    state = application.state
    if state.canary_scheduler:
        try:
            await run_sync(state.canary_scheduler.shutdown, wait=False)
        except Exception as exc:
            logger.warning("Error stopping CanaryScheduler: %s", exc)
    if state.realtime_worker:
        try:
            await state.realtime_worker.stop()
        except Exception as exc:
            logger.warning("Error stopping RealtimeWorker: %s", exc)
    if state.query_service:
        await state.query_service.close()
    if state.redis_client:
        await state.redis_client.aclose()
    if state.cache_service and state.cache_service.client:
        await run_sync(state.cache_service.client.close)
    if state.async_db_resources:
        await state.async_db_resources.close()
    if state.db_service:
        await run_sync(state.db_service.close)

    for state_name in (
        "canary_scheduler",
        "canary_monitor",
        "validation_layer",
        "realtime_worker",
        "news_service",
        "dcf_service",
        "technical_service",
        "ingestion_service",
        "financials_service",
        "cache_service",
        "redis_client",
        "query_service",
        "db_service",
        "async_session_factory",
        "async_db_resources",
    ):
        setattr(state, state_name, None)
    state.db_available = None
    logger.info("🛑 FonRex API stopped")


@app.get("/")
async def index():
    """Page d'accueil avec documentation de l'API."""
    documentation = get_api_documentation()
    return documentation
