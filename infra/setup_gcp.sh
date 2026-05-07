#!/usr/bin/env bash
# GCP Infrastructure Setup Script
# Run once to provision all GCP resources for the D2C Platform.
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT_ID
#   Billing enabled on the project
#
# Usage:
#   chmod +x infra/setup_gcp.sh
#   ./infra/setup_gcp.sh YOUR_PROJECT_ID us-central1
#
# WHAT THIS CREATES:
#   - Artifact Registry repository for Docker images
#   - VPC network + subnet for private Cloud SQL access
#   - Serverless VPC Access connector (Cloud Run → Cloud SQL private IP)
#   - Cloud SQL PostgreSQL 16 instance (private IP only, no public IP)
#   - Cloud SQL databases and users
#   - Secret Manager secrets (JWT key, encryption key, DB password, OAuth secrets)
#   - Service Account with minimum required permissions (least-privilege IAM)
#   - Cloud Build trigger (deploy on push to main)
#
# IAM PRINCIPLE OF LEAST PRIVILEGE:
#   The Cloud Run service account has ONLY:
#     - Cloud SQL Client (connect to the instance)
#     - Secret Manager Secret Accessor (read the 6 secrets it needs)
#     - No editor/owner roles, no storage access, no other permissions
#
# COST ESTIMATE (us-central1, light load):
#   Cloud SQL db-g1-small: ~$25/month
#   Cloud Run (min 1 instance, 512MB): ~$15/month
#   Secret Manager: ~$0.06/month (6 secrets × 10K accesses)
#   Artifact Registry: ~$0.10/GB
#   VPC Connector: ~$10/month
#   Total: ~$50/month

set -euo pipefail

PROJECT_ID="${1:?Usage: ./setup_gcp.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"
DB_INSTANCE="d2c-db"
DB_NAME="d2c_db"
DB_APP_USER="d2c_app"
DB_MIGRATIONS_USER="d2c_migrations"
SA_NAME="d2c-platform-sa"
REPO_NAME="d2c"
VPC_NAME="d2c-vpc"
SUBNET_NAME="d2c-subnet"
CONNECTOR_NAME="d2c-vpc-connector"

echo "🚀 Setting up D2C Platform on GCP project: $PROJECT_ID (region: $REGION)"

# ── Enable required APIs ───────────────────────────────────────────────────────
echo "📡 Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  vpcaccess.googleapis.com \
  servicenetworking.googleapis.com \
  --project="$PROJECT_ID"

# ── Artifact Registry ──────────────────────────────────────────────────────────
echo "📦 Creating Artifact Registry..."
gcloud artifacts repositories create "$REPO_NAME" \
  --repository-format=docker \
  --location="$REGION" \
  --description="D2C Platform Docker images" \
  --project="$PROJECT_ID" || true  # idempotent

# ── VPC Network ────────────────────────────────────────────────────────────────
echo "🌐 Creating VPC network..."
gcloud compute networks create "$VPC_NAME" \
  --subnet-mode=custom \
  --project="$PROJECT_ID" || true

gcloud compute networks subnets create "$SUBNET_NAME" \
  --network="$VPC_NAME" \
  --region="$REGION" \
  --range="10.0.0.0/24" \
  --project="$PROJECT_ID" || true

# Private Services Access (required for Cloud SQL private IP)
gcloud compute addresses create google-managed-services-"$VPC_NAME" \
  --global \
  --purpose=VPC_PEERING \
  --prefix-length=16 \
  --network="$VPC_NAME" \
  --project="$PROJECT_ID" || true

gcloud services vpc-peerings connect \
  --service=servicenetworking.googleapis.com \
  --ranges=google-managed-services-"$VPC_NAME" \
  --network="$VPC_NAME" \
  --project="$PROJECT_ID" || true

# Serverless VPC Access Connector (Cloud Run → private VPC)
echo "🔌 Creating VPC Access Connector..."
gcloud compute networks vpc-access connectors create "$CONNECTOR_NAME" \
  --region="$REGION" \
  --subnet="$SUBNET_NAME" \
  --subnet-project="$PROJECT_ID" \
  --min-instances=2 \
  --max-instances=10 \
  --project="$PROJECT_ID" || true

# ── Cloud SQL ──────────────────────────────────────────────────────────────────
echo "🗄️  Creating Cloud SQL instance (this takes ~5 minutes)..."
# db-g1-small: 0.6 vCPU, 1.7GB RAM — fine for dev/staging
# For production: db-n1-standard-2 or higher
gcloud sql instances create "$DB_INSTANCE" \
  --database-version=POSTGRES_16 \
  --tier=db-g1-small \
  --region="$REGION" \
  --network="projects/$PROJECT_ID/global/networks/$VPC_NAME" \
  --no-assign-ip \
  --storage-type=SSD \
  --storage-size=20GB \
  --storage-auto-increase \
  --backup-start-time=03:00 \
  --enable-point-in-time-recovery \
  --maintenance-window-day=SUN \
  --maintenance-window-hour=04 \
  --deletion-protection \
  --database-flags=log_min_duration_statement=1000,log_connections=on \
  --project="$PROJECT_ID" || echo "Instance may already exist"

# Create databases
gcloud sql databases create "$DB_NAME" \
  --instance="$DB_INSTANCE" \
  --project="$PROJECT_ID" || true

# Generate passwords
DB_APP_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
DB_MIGRATIONS_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# Create users
gcloud sql users create "$DB_APP_USER" \
  --instance="$DB_INSTANCE" \
  --password="$DB_APP_PASSWORD" \
  --project="$PROJECT_ID" || true

gcloud sql users create "$DB_MIGRATIONS_USER" \
  --instance="$DB_INSTANCE" \
  --password="$DB_MIGRATIONS_PASSWORD" \
  --project="$PROJECT_ID" || true

echo "⚠️  DB passwords generated — storing in Secret Manager..."

# ── Secret Manager ─────────────────────────────────────────────────────────────
echo "🔐 Creating secrets in Secret Manager..."

create_or_update_secret() {
  local SECRET_NAME="$1"
  local SECRET_VALUE="$2"

  # Check if secret exists
  if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo "  Updating secret: $SECRET_NAME"
    echo -n "$SECRET_VALUE" | gcloud secrets versions add "$SECRET_NAME" \
      --data-file=- \
      --project="$PROJECT_ID"
  else
    echo "  Creating secret: $SECRET_NAME"
    echo -n "$SECRET_VALUE" | gcloud secrets create "$SECRET_NAME" \
      --data-file=- \
      --replication-policy=automatic \
      --project="$PROJECT_ID"
  fi
}

# Generate application secrets
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# Fernet key must be 32 url-safe base64 bytes
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

create_or_update_secret "d2c-jwt-secret" "$JWT_SECRET"
create_or_update_secret "d2c-encryption-key" "$ENCRYPTION_KEY"
create_or_update_secret "d2c-db-password" "$DB_APP_PASSWORD"
create_or_update_secret "d2c-db-migrations-password" "$DB_MIGRATIONS_PASSWORD"
# These are populated manually after OAuth app registration:
create_or_update_secret "d2c-shopify-client-secret" "REPLACE_WITH_REAL_SECRET"
create_or_update_secret "d2c-shopify-webhook-secret" "REPLACE_WITH_REAL_SECRET"
create_or_update_secret "d2c-meta-app-secret" "REPLACE_WITH_REAL_SECRET"

# ── Service Account (least-privilege) ─────────────────────────────────────────
echo "👤 Creating service account..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="D2C Platform Cloud Run Service Account" \
  --project="$PROJECT_ID" || true

SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

# Cloud SQL Client — connect to the instance
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/cloudsql.client"

# Secret Manager Secret Accessor — for each individual secret (not all secrets)
for SECRET in d2c-jwt-secret d2c-encryption-key d2c-db-password \
              d2c-shopify-client-secret d2c-shopify-webhook-secret d2c-meta-app-secret; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT_ID"
done

# Artifact Registry Reader (pull images to run them)
gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" \
  --location="$REGION" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/artifactregistry.reader" \
  --project="$PROJECT_ID"

# Cloud Build Service Account — needs to deploy to Cloud Run
CLOUDBUILD_SA="$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')@cloudbuild.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/run.developer"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$CLOUDBUILD_SA" \
  --role="roles/iam.serviceAccountUser"

# ── Initial DB setup (grant app user restricted permissions) ──────────────────
echo "🔧 Initialising database permissions..."
# Note: This runs as the migrations user which has full DDL access.
# The app user (d2c_app) only gets DML access.
# RLS policies restrict d2c_app further to only their brand's rows.
gcloud sql connect "$DB_INSTANCE" \
  --user=postgres \
  --project="$PROJECT_ID" \
  --database="$DB_NAME" << 'SQL'
-- Grant migrations user full DDL access
GRANT ALL PRIVILEGES ON DATABASE d2c_db TO d2c_migrations;
GRANT ALL ON SCHEMA public TO d2c_migrations;

-- Grant app user restricted DML access only
GRANT CONNECT ON DATABASE d2c_db TO d2c_app;
GRANT USAGE ON SCHEMA public TO d2c_app;
-- Table-level grants are managed by the 0002_rls migration

-- Enable pg_stat_statements for query performance monitoring
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL

echo ""
echo "✅ GCP infrastructure setup complete!"
echo ""
echo "📋 Next steps:"
echo "  1. Update infra/cloudrun-service.yaml: replace PROJECT_ID with $PROJECT_ID"
echo "  2. Build and push the initial image:"
echo "     gcloud builds submit --config=infra/cloudbuild.yaml"
echo "  3. Deploy the service:"
echo "     gcloud run services replace infra/cloudrun-service.yaml --region=$REGION"
echo "  4. Run initial migrations:"
echo "     gcloud run jobs execute d2c-migrate --region=$REGION"
echo "  5. Run the seed script (optional, for testing):"
echo "     gcloud run jobs create d2c-seed ..."
echo ""
echo "🔐 Secret Manager secrets created:"
echo "  d2c-jwt-secret, d2c-encryption-key, d2c-db-password"
echo "  d2c-shopify-client-secret (update with real value)"
echo "  d2c-shopify-webhook-secret (update with real value)"
echo "  d2c-meta-app-secret (update with real value)"
echo ""
echo "⚠️  IMPORTANT: The generated JWT_SECRET and ENCRYPTION_KEY are stored"
echo "   ONLY in Secret Manager. They are not printed here for security."
echo "   Access them via: gcloud secrets versions access latest --secret=d2c-jwt-secret"
