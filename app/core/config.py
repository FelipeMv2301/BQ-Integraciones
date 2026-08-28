"""
Configuración central del servicio.

Todas las variables de entorno del proyecto se definen aquí. Pydantic
Settings las lee de .env (local) o de las variables inyectadas por el
entorno de despliegue (producción). Si falta una variable obligatoria al
arrancar, Python lanza un error claro indicando cuál falta.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Base de datos (PostgreSQL, base propia — sin schema compartido) ────
    DATABASE_URL: str

    # ── Redis (broker + result backend de Celery + caché SAP/Stock-Service) ─
    REDIS_URL: str

    @property
    def redis_url(self) -> str:
        """REDIS_URL lista para clientes. Si es rediss:// sin ssl_cert_reqs, se agrega
        en minúsculas (redis-py solo acepta none/optional/required)."""
        url = self.REDIS_URL
        if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}ssl_cert_reqs=none"
        return url

    # ── Token-SAP-BQ — sesión SAP compartida ─────────────────────────────────
    # Nunca se hace POST /Login directo contra SAP — solo /session y
    # /session/invalidate de Token-SAP-BQ (ver I5 en BACKLOG.md).
    TOKEN_SAP_BQ_URL: str
    TOKEN_SAP_BQ_SERVICE_NAME: str
    TOKEN_SAP_BQ_PASSWORD: str

    # ── SAP Business One — Service Layer ────────────────────────────────────
    SAP_URL: str
    SAP_TLS_VERIFY: str = "true"

    @property
    def sap_tls_verify(self) -> bool | str:
        v = self.SAP_TLS_VERIFY.strip().lower()
        if v in ("false", "0", "no"):
            return False
        if v in ("true", "1", "yes"):
            return True
        return self.SAP_TLS_VERIFY  # ruta a bundle CA

    SAP_TIMEOUT_REQUEST: int = 60
    SAP_TIMEOUT_PAGINATED: int = 120

    # ── Stock-Service — catálogo de productos (SKU → bodega/precio) ─────────
    STOCK_SERVICE_URL: str
    STOCK_SERVICE_API_KEY: str
    STOCK_SERVICE_CACHE_TTL_SECONDS: int = 900  # 15 min, ver plan.md

    # ── WooCommerce ──────────────────────────────────────────────────────────
    WOO_URL: str
    WOO_KEY: str
    WOO_SECRET: str
    WOO_POLL_INTERVAL_MINUTES: int = 5
    # Ventana hacia atrás para modified_after — mismo patrón que Integrify-Consola
    # (ventana fija recalculada cada corrida, sin checkpoint persistido; el dedup
    # por `code` en poll_woo_orders() ya protege el solape). 1 día da margen de
    # sobra corriendo cada 5 min, sin volver a traer la tienda entera.
    WOO_POLL_LOOKBACK_DAYS: int = 1

    # ── WooCommerce — sitio nuevo (bioquimica.devwebs.cl, transicional) ─────
    # Mientras se resuelve el permiso del endpoint normalizado de BioCommerce
    # PRO, leemos la API nativa de WooCommerce de este sitio directo.
    WOO_NUEVO_URL: str = ""
    WOO_NUEVO_KEY: str = ""
    WOO_NUEVO_SECRET: str = ""

    # ── Facele / Docele (SOAP) ───────────────────────────────────────────────
    FACELE_URL: str
    FACELE_USER: str
    FACELE_PASSWORD: str
    FACELE_TAXID: str

    # ── Brevo ────────────────────────────────────────────────────────────────
    BREVO_URL: str = "https://api.brevo.com/v3/"
    BREVO_API_KEY: str
    BREVO_TEMPLATE_CUSTOMER_INVOICE: str
    BREVO_SENDER_NAME: str
    BREVO_SENDER_EMAIL: str
    ALERT_EMAILS: str = ""
    BREVO_INVOICE_BCC: str = ""  # copia interna de cada factura enviada al cliente (R5)

    @property
    def alert_recipients(self) -> list[str]:
        return [e.strip() for e in self.ALERT_EMAILS.split(",") if e.strip()]

    @property
    def invoice_bcc_recipients(self) -> list[str]:
        return [e.strip() for e in self.BREVO_INVOICE_BCC.split(",") if e.strip()]

    # ── SMTP directo — alertas internas de error (notify_failure) ──────────
    # A propósito NO usa Brevo: Brevo es para comunicación con clientes
    # (CUSTOMER_INVOICE), esto es solo detección interna del equipo.
    EMAIL_SMTP_HOST: str = "smtp.gmail.com"
    EMAIL_SMTP_PORT: int = 587
    EMAIL_SENDER: str = ""
    EMAIL_PASSWORD: str = ""

    # ── Polling y circuit breaker (I3) ──────────────────────────────────────
    SAP_INVOICE_POLL_INTERVAL_MINUTES: int = 10
    MAX_ORDERS_PER_CYCLE: int = 50

    # ── Reintentos por entidad (ver BACKLOG.md §7/§9) ───────────────────────
    RESOLVE_CUSTOMER_MAX_ATTEMPTS: int = 10
    SAP_BILLING_MAX_ATTEMPTS: int = 10
    FACELE_MAX_ATTEMPTS: int = 5
    EMAIL_MAX_ATTEMPTS: int = 3

    # ── Seguridad API ────────────────────────────────────────────────────────
    API_KEY: str = ""

    # ── Observabilidad — Sentry ──────────────────────────────────────────────
    SENTRY_DSN: str = ""
    SENTRY_RELEASE: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_DEBUG: bool = False

    @property
    def sentry_enabled(self) -> bool:
        return bool(self.SENTRY_DSN)

    # ── Observabilidad — Healthchecks.io (heartbeat de Beat, BQI-62) ────────
    # "slug:uuid,slug:uuid" — un check por tarea, creado a mano en el
    # dashboard. El auto-provisioning por ping-key (URL /ping-key/slug) no
    # funcionó en esta cuenta (404 "not found" pese a key válida) — se usa
    # el ping directo por UUID de cada check, que sí confirmado funciona.
    HEALTHCHECKS_CHECKS: str = ""
    HEALTHCHECKS_BASE_URL: str = "https://hc-ping.com"
    HEALTHCHECKS_TIMEOUT: int = 5

    @property
    def healthchecks_checks(self) -> dict[str, str]:
        resultado = {}
        for par in self.HEALTHCHECKS_CHECKS.split(","):
            par = par.strip()
            if not par:
                continue
            slug, _, uuid = par.partition(":")
            if slug and uuid:
                resultado[slug] = uuid
        return resultado

    @property
    def healthchecks_enabled(self) -> bool:
        return bool(self.healthchecks_checks)

    # ── Entorno ──────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    TZ: str = "America/Santiago"
    LOG_LEVEL: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


# Instancia global — importar desde acá en todo el proyecto:
#   from app.core.config import settings
settings = Settings()