
set -euo pipefail

PROJECT_ID="${1:?Usage: ./setup_deploy_iam.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"
SA_NAME="d2c-cicd-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_NAME="d2c"

echo "Creating CI/CD service account: ${SA_EMAIL}"

# ── Create service account ────────────────────────────────────────────────────
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="D2C CI/CD Deploy Service Account (GitHub Actions)" \
  --description="Used by GitHub Actions to push/pull Docker images only" \
  --project="${PROJECT_ID}" 2>/dev/null || echo "SA already exists"

# ── Grant ONLY Artifact Registry permissions ──────────────────────────────────
# Writer = can push images (needed by GitHub Actions build job)
gcloud artifacts repositories add-iam-policy-binding "${REPO_NAME}" \
  --location="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer" \
  --project="${PROJECT_ID}"

# Reader = can pull images (needed by the UAT server via Ansible)
# The UAT server uses the SAME SA key with reader rights to pull.
# Artifact Registry Writer includes Reader, so this is redundant but explicit.
gcloud artifacts repositories add-iam-policy-binding "${REPO_NAME}" \
  --location="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.reader" \
  --project="${PROJECT_ID}"

echo ""
echo "Permissions granted:"
gcloud artifacts repositories get-iam-policy "${REPO_NAME}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="table(bindings.role,bindings.members)"

# ── Generate and download a key ───────────────────────────────────────────────
KEY_FILE="/tmp/${SA_NAME}-key.json"

gcloud iam service-accounts keys create "${KEY_FILE}" \
  --iam-account="${SA_EMAIL}" \
  --project="${PROJECT_ID}"

echo ""
echo "============================================================"
echo "SA key created: ${KEY_FILE}"
echo ""
echo "ADD THIS TO GITHUB SECRETS as GCP_SA_KEY:"
echo "(Settings → Secrets and variables → Actions → New repository secret)"
echo ""
echo "--- COPY THE ENTIRE OUTPUT BELOW ---"
base64 -w 0 "${KEY_FILE}"
echo ""
echo "--- END ---"
echo ""
echo "THEN DELETE THE LOCAL KEY FILE:"
echo "  rm ${KEY_FILE}"
echo ""
echo "The SA key is now stored in GitHub Secrets only."
echo "============================================================"

# Verify the SA cannot access secrets (should return permission denied)
echo ""
echo "Verifying SA cannot access Secret Manager (expected: PERMISSION_DENIED)..."
if gcloud secrets versions access latest \
    --secret=d2c-jwt-secret \
    --impersonate-service-account="${SA_EMAIL}" \
    --project="${PROJECT_ID}" 2>&1 | grep -q "PERMISSION_DENIED"; then
  echo "PASS: CI/CD SA correctly denied access to Secret Manager"
else
  echo "WARNING: Check IAM — CI/CD SA may have unexpected permissions"
fi