#!/usr/bin/env bash
# ============================================================
# bootstrap_uat_server.sh — One-time UAT server setup
# ============================================================
# Run this ONCE on a fresh Ubuntu 22.04/24.04 server before
# the first CI/CD deploy.
#
# Usage:
#   ssh ubuntu@YOUR_UAT_SERVER_IP
#   curl -sO https://raw.githubusercontent.com/YOUR_ORG/d2c-platform/uat/infra/bootstrap_uat_server.sh
#   chmod +x bootstrap_uat_server.sh
#   ./bootstrap_uat_server.sh YOUR_GCP_PROJECT_ID us-central1

set -euo pipefail

PROJECT_ID="${1:?Usage: ./bootstrap_uat_server.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"

echo "=== Bootstrapping UAT server ==="

# ── Install Docker ────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | bash
  sudo usermod -aG docker "$USER"
  echo "Docker installed. You may need to log out and back in for group changes."
else
  echo "Docker already installed: $(docker --version)"
fi

# ── Install GCloud SDK ────────────────────────────────────────────────────────
if ! command -v gcloud &>/dev/null; then
  echo "Installing Google Cloud SDK..."
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] \
    https://packages.cloud.google.com/apt cloud-sdk main" \
    | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
  sudo apt-get update -q && sudo apt-get install -y google-cloud-cli
else
  echo "gcloud already installed: $(gcloud --version | head -1)"
fi

# ── Create app directories ────────────────────────────────────────────────────
sudo mkdir -p /opt/d2c/logs
sudo chown -R "$USER:$USER" /opt/d2c
chmod 755 /opt/d2c
chmod 755 /opt/d2c/logs

# ── Docker network ────────────────────────────────────────────────────────────
docker network create d2c-network 2>/dev/null || echo "Network d2c-network already exists"

# ── Start PostgreSQL container ────────────────────────────────────────────────
# Generate a random DB password
DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

if ! docker ps -a --format '{{.Names}}' | grep -q '^d2c-postgres$'; then
  echo "Starting PostgreSQL container..."
  docker run \
    --detach \
    --name d2c-postgres \
    --restart unless-stopped \
    --network d2c-network \
    -e POSTGRES_USER=d2c \
    -e POSTGRES_PASSWORD="${DB_PASSWORD}" \
    -e POSTGRES_DB=d2c_db \
    -v postgres_data:/var/lib/postgresql/data \
    postgres:16-alpine
  echo "PostgreSQL started"
  echo "DB_PASSWORD=${DB_PASSWORD}"
  echo ""
  echo "SAVE THIS PASSWORD — you'll need it in /opt/d2c/.env.uat"
else
  echo "PostgreSQL already running"
  DB_PASSWORD="ALREADY_SET_CHECK_EXISTING_ENV"
fi

# ── Start Mock Shopify server ─────────────────────────────────────────────────
# These are baked into the app image — run them from the image directly
# or use a separate lightweight image. For UAT simplicity, we run them
# from the app image itself using a different command.

echo ""
echo "=== Setup complete ==="
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Create /opt/d2c/.env.uat (copy from ansible/.env.uat.template in the repo):"
echo "   sudo nano /opt/d2c/.env.uat"
echo "   sudo chmod 600 /opt/d2c/.env.uat"
echo ""
echo "2. Fill in the secrets. Fetch from GCP Secret Manager:"
echo "   gcloud auth activate-service-account --key-file=/tmp/gcp-sa-key.json"
echo "   gcloud secrets versions access latest --secret=d2c-jwt-secret --project=${PROJECT_ID}"
echo "   gcloud secrets versions access latest --secret=d2c-encryption-key --project=${PROJECT_ID}"
echo "   gcloud secrets versions access latest --secret=d2c-db-password --project=${PROJECT_ID}"
echo "   (and the 3 OAuth secrets)"
echo ""
echo "3. Set the database password in .env.uat:"
echo "   DB_PASSWORD=${DB_PASSWORD}"
echo "   DATABASE_URL=postgresql+asyncpg://d2c:${DB_PASSWORD}@d2c-postgres:5432/d2c_db"
echo ""
echo "4. Push to the uat branch to trigger the first deploy:"
echo "   git push origin uat"
echo ""
echo "5. After first deploy, start mock servers:"
echo "   docker run -d --name mock-shopify --network d2c-network -p 8001:8001 \\"
echo "     IMAGE_FROM_REGISTRY \\"
echo "     uvicorn mock_servers.shopify_mock:mock_shopify --host 0.0.0.0 --port 8001"
echo ""
echo "   docker run -d --name mock-meta --network d2c-network -p 8002:8002 \\"
echo "     IMAGE_FROM_REGISTRY \\"
echo "     uvicorn mock_servers.meta_mock:mock_meta --host 0.0.0.0 --port 8002"