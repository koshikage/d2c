"""
GCP Secret Manager Integration
================================
Fetches production secrets from GCP Secret Manager and injects them
into the Settings object (Phase 2 of config loading).

CALLED BY:
  app/core/config.py → build_settings() → load_production_secrets(settings)
  This runs once in the FastAPI lifespan, before the app accepts requests.

WHY A SEPARATE FILE:
  - secret_manager.py is ONLY imported in production (ENVIRONMENT=production).
    In development and tests, google-cloud-secret-manager is never imported,
    so you don't need the GCP SDK installed locally unless you want to.
  - Keeping Secret Manager logic here means config.py stays clean and testable
    without mocking GCP APIs.

HOW THE GCP SDK AUTHENTICATES:
  In Cloud Run: Application Default Credentials (ADC) automatically uses the
  Cloud Run service account. No key file needed. The service account must have
  roles/secretmanager.secretAccessor on each secret (see setup_gcp.sh).

  Locally (if you ever want to test against real secrets):
    gcloud auth application-default login
  This writes credentials to ~/.config/gcloud/application_default_credentials.json
  which the SDK finds automatically.

SECRET NAMING CONVENTION:
  All secrets in this project follow: d2c-{purpose}
  Examples: d2c-jwt-secret, d2c-encryption-key, d2c-db-password
  This prefix makes it easy to find all app secrets in the GCP console and
  to write IAM policies that grant access to "d2c-*" secrets only.

SECRET VERSIONS:
  We always fetch the "latest" version. To pin to a specific version:
    projects/PROJECT/secrets/d2c-jwt-secret/versions/3
  Pinning is useful after a rotation when you want to verify the new version
  works before decommissioning the old one.

WHAT HAPPENS IF A SECRET IS MISSING:
  SecretNotFound → startup fails with a clear error message that names the
  missing secret. The app never starts in a partially-configured state.
  This is intentional: a missing secret in production is a deployment error,
  not something to silently work around.
"""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Mapping: Settings field name → GCP Secret Manager secret name ─────────────
#
# Left side:  the exact attribute name on the Settings class
# Right side: the secret name in GCP Secret Manager
#
# To add a new secret:
#   1. Add the field to Settings in config.py as SecretStr
#   2. Add the mapping here
#   3. Create the secret: gcloud secrets create d2c-new-secret ...
#   4. Grant access to the Cloud Run service account
#   5. Add to .env.example and your local .env

SECRET_MANAGER_MAPPINGS: dict[str, str] = {
    "JWT_SECRET_KEY":          "d2c-jwt-secret",
    "ENCRYPTION_KEY":          "d2c-encryption-key",
    "SHOPIFY_CLIENT_SECRET":   "d2c-shopify-client-secret",
    "SHOPIFY_WEBHOOK_SECRET":  "d2c-shopify-webhook-secret",
    "META_APP_SECRET":         "d2c-meta-app-secret",
    "DB_PASSWORD":             "d2c-db-password",
}


async def fetch_secret(project_id: str, secret_name: str, version: str = "latest") -> str:
    """
    Fetch a single secret value from GCP Secret Manager.

    project_id:  GCP project ID (e.g. "my-project-123")
    secret_name: the secret's name in Secret Manager (e.g. "d2c-jwt-secret")
    version:     "latest" or a specific version number like "3"

    Returns the secret value as a plain string.
    Raises google.api_core.exceptions.NotFound if the secret doesn't exist.

    WHY async + run_in_executor:
      The google-cloud-secret-manager SDK is synchronous. We wrap the blocking
      call in run_in_executor so it doesn't block the FastAPI event loop during
      startup. For 6 secrets fetched sequentially this adds ~200ms to startup,
      which is acceptable. For faster startup, fetch them concurrently with
      asyncio.gather (see load_production_secrets below).
    """
    from google.cloud import secretmanager  # type: ignore[import]

    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/{version}"

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,                                   # default thread pool
        lambda: client.access_secret_version(name=secret_path),
    )

    secret_value = response.payload.data.decode("utf-8").strip()

    logger.info(
        "secret_fetched",
        secret_name=secret_name,
        version=version,
        # Never log the value — only confirm it was fetched
    )
    return secret_value


async def load_production_secrets(settings: "Settings") -> None:
    """
    Phase 2 of config loading (production only).

    Fetches all secrets from GCP Secret Manager concurrently and
    updates the Settings object in place using object.__setattr__
    (bypasses Pydantic's immutability for this one-time initialisation).

    Also assembles the Cloud SQL DATABASE_URL from the fetched DB password
    and the Cloud SQL socket path.

    This function is called once at startup by build_settings() in config.py.
    After it returns, settings.JWT_SECRET_KEY.get_secret_value() returns
    the real production value everywhere in the app.

    WHY object.__setattr__ instead of creating a new Settings instance:
      Creating a new Settings() would re-run Pydantic validation and
      re-read from the env file. Using __setattr__ directly updates the
      cached singleton in place, which is what we want: one object,
      all code that imported `settings` gets the updated values.
    """
    from pydantic import SecretStr

    project_id = settings.GCP_PROJECT_ID
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID is not set. "
            "Add it as a non-secret env var in your Cloud Run service YAML."
        )

    logger.info("loading_production_secrets", project_id=project_id, count=len(SECRET_MANAGER_MAPPINGS))

    # Fetch all secrets concurrently for fast startup
    # asyncio.gather runs all fetch_secret coroutines in parallel
    secret_names = list(SECRET_MANAGER_MAPPINGS.keys())
    gcp_names = list(SECRET_MANAGER_MAPPINGS.values())

    try:
        values = await asyncio.gather(
            *[fetch_secret(project_id, gcp_name) for gcp_name in gcp_names]
        )
    except Exception as e:
        logger.error("secret_loading_failed", error=str(e))
        raise RuntimeError(
            f"Failed to load production secrets from GCP Secret Manager: {e}\n"
            "Check that:\n"
            "  1. GCP_PROJECT_ID env var is set correctly in Cloud Run\n"
            "  2. The Cloud Run service account has secretmanager.secretAccessor\n"
            "  3. All secrets in SECRET_MANAGER_MAPPINGS exist in your project\n"
            "  4. The VPC connector is configured if using private Cloud SQL"
        ) from e

    # Inject fetched values into the Settings singleton
    for field_name, value in zip(secret_names, values):
        object.__setattr__(settings, field_name, SecretStr(value))

    logger.info("secrets_loaded", count=len(secret_names))

    # Assemble the Cloud SQL DATABASE_URL using the fetched DB password
    # Cloud SQL Proxy Unix socket path: /cloudsql/PROJECT:REGION:INSTANCE
    _assemble_cloud_sql_urls(settings)


def _assemble_cloud_sql_urls(settings: "Settings") -> None:
    """
    Build the async and sync DATABASE_URLs for Cloud SQL.

    In production, the DB password comes from Secret Manager (now loaded).
    The hostname is a Unix socket path managed by the Cloud SQL Auth Proxy,
    not a TCP host:port. This means:
      - No network port opened (more secure)
      - IAM authentication handled by the Proxy
      - No SSL certificate management needed

    Unix socket path format (asyncpg): ?host=/cloudsql/PROJECT:REGION:INSTANCE
    Unix socket path format (psycopg2): host=/cloudsql/PROJECT:REGION:INSTANCE (in query string)

    If CLOUD_SQL_INSTANCE is not set (e.g. running on a plain VPS, not Cloud Run),
    the DATABASE_URL is left as-is from the env var.
    """
    if not settings.CLOUD_SQL_INSTANCE:
        logger.info("cloud_sql_url_skipped", reason="CLOUD_SQL_INSTANCE not set, using DATABASE_URL as-is")
        return

    db_password = settings.DB_PASSWORD.get_secret_value()
    db_user = settings.CLOUD_SQL_DB_USER
    db_name = settings.CLOUD_SQL_DB_NAME
    socket_path = f"/cloudsql/{settings.CLOUD_SQL_INSTANCE}"

    # asyncpg (used by the app at runtime)
    async_url = (
        f"postgresql+asyncpg://{db_user}:{db_password}@/{db_name}"
        f"?host={socket_path}"
    )

    # psycopg2 (used by Alembic for migrations only)
    sync_url = (
        f"postgresql+psycopg2://{db_user}:{db_password}@/{db_name}"
        f"?host={socket_path}"
    )

    object.__setattr__(settings, "DATABASE_URL", async_url)
    object.__setattr__(settings, "DATABASE_SYNC_URL", sync_url)

    logger.info(
        "cloud_sql_url_assembled",
        instance=settings.CLOUD_SQL_INSTANCE,
        db_user=db_user,
        db_name=db_name,
        # Never log the password or the full URL (contains password)
    )


# ── Utility functions for managing secrets (run these from your terminal) ──────

def create_secret(project_id: str, secret_name: str, secret_value: str) -> None:
    """
    Create a new secret in GCP Secret Manager.

    Run this from your terminal (not in the app) when setting up a new deployment.

    Usage:
        python -c "
        from app.core.secret_manager import create_secret
        create_secret('my-project', 'd2c-jwt-secret', 'your-secret-value')
        "
    """
    from google.cloud import secretmanager  # type: ignore[import]
    from google.api_core.exceptions import AlreadyExists

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project_id}"

    try:
        secret = client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
    except AlreadyExists:

    # Add the secret value as version 1 (or next version if already exists)
    secret_path = f"projects/{project_id}/secrets/{secret_name}"
    version = client.add_secret_version(
        request={
            "parent": secret_path,
            "payload": {"data": secret_value.encode("utf-8")},
        }
    )
    print(f"Added secret version: {version.name}")


def rotate_secret(project_id: str, secret_name: str, new_value: str) -> None:
    """
    Add a new version of an existing secret (rotation).

    The old version remains accessible until you disable or destroy it.
    This enables zero-downtime rotation: the running app uses the old version
    until you redeploy with the new version.

    For ENCRYPTION_KEY rotation, see token_lifecycle.py for the MultiFernet
    approach (comma-separated keys, old and new both valid during transition).
    """
    from google.cloud import secretmanager  # type: ignore[import]

    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/{project_id}/secrets/{secret_name}"

    version = client.add_secret_version(
        request={
            "parent": secret_path,
            "payload": {"data": new_value.encode("utf-8")},
        }
    )
    print(f"Rotated secret {secret_name}: new version is {version.name}")
    print("Previous version is still active. Redeploy to use the new version.")
    print(f"To disable old version: gcloud secrets versions disable {version.name.rsplit('/', 2)[0] + '/versions/' + str(int(version.name.rsplit('/', 1)[1]) - 1)} --secret={secret_name} --project={project_id}")


def grant_service_account_access(project_id: str, service_account_email: str) -> None:
    """
    Grant a service account access to all d2c secrets.

    Run this after creating a new Cloud Run service account, or when adding
    a new secret to SECRET_MANAGER_MAPPINGS.

    Grants roles/secretmanager.secretAccessor on each individual secret
    (NOT project-wide — principle of least privilege).
    """
    from google.cloud import secretmanager  # type: ignore[import]

    client = secretmanager.SecretManagerServiceClient()
    member = f"serviceAccount:{service_account_email}"

    for gcp_secret_name in SECRET_MANAGER_MAPPINGS.values():
        secret_path = f"projects/{project_id}/secrets/{gcp_secret_name}"

        policy = client.get_iam_policy(request={"resource": secret_path})
        policy.bindings.add(
            role="roles/secretmanager.secretAccessor",
            members=[member],
        )
        client.set_iam_policy(request={"resource": secret_path, "policy": policy})
        print(f"Granted {service_account_email} access to {gcp_secret_name}")


def list_secrets(project_id: str) -> None:
    """List all d2c secrets and their latest version status."""
    from google.cloud import secretmanager  # type: ignore[import]

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project_id}"

    for secret in client.list_secrets(request={"parent": parent}):
        name = secret.name.split("/")[-1]
        if name.startswith("d2c-"):
            try:
                version_path = f"{secret.name}/versions/latest"
                version = client.get_secret_version(request={"name": version_path})
                status = version.state.name   # ENABLED, DISABLED, DESTROYED
                created = version.create_time.strftime("%Y-%m-%d %H:%M")
            except Exception:
                status = "NO VERSIONS"
                created = "—"
            print(f"  {name:<35} {status:<10} created: {created}")