# Credentials & Secrets Guide
## D2C Platform — End-to-End CI/CD Setup

Everything you need to create before the pipeline works, where each credential
lives, and exactly how to generate it.

---

## Overview — What credentials exist and where they live

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CREDENTIAL MAP                                 │
│                                                                     │
│  GitHub Secrets             GCP Secret Manager    UAT Server disk  │
│  ─────────────────          ──────────────────    ────────────────  │
│  GCP_PROJECT_ID             d2c-jwt-secret        /opt/d2c/.env.uat│
│  GCP_REGION                 d2c-encryption-key                     │
│  GCP_SA_KEY ──────────────► d2c-db-password                        │
│  (deploy SA key,            d2c-shopify-client-secret              │
│   Artifact Registry         d2c-shopify-webhook-secret             │
│   Writer only)              d2c-meta-app-secret                    │
│                                                                     │
│  UAT_SERVER_HOST                                                    │
│  UAT_SERVER_USER                                                    │
│  UAT_SSH_PRIVATE_KEY ──────────────────────────► server SSH access │
└─────────────────────────────────────────────────────────────────────┘
```

---

## PART 1 — GitHub Secrets (7 values)

Go to: GitHub → Your Repo → Settings → Secrets and variables → Actions → New repository secret

### GCP_PROJECT_ID
**What**: Your GCP project ID (not project number, not project name).
**Format**: `my-project-abc-123` (lowercase, hyphens)
**How to find**:
```bash
gcloud projects list
# Or check console.cloud.google.com — top left dropdown
```
**Value**: your actual GCP project ID, e.g. `d2c-platform-prod`

---

### GCP_REGION
**What**: The GCP region where your Artifact Registry and Cloud Run live.
**Format**: `us-central1` (or `asia-south1`, `europe-west1`, etc.)
**Value**: `us-central1` (unless you chose a different region in setup_gcp.sh)

---

### GCP_SA_KEY
**What**: A base64-encoded JSON key for the CI/CD service account.
This SA can ONLY push/pull Docker images. It cannot read app secrets,
access Cloud SQL, or deploy Cloud Run. This is intentional.

**How to generate** (run setup_deploy_iam.sh):
```bash
# On your local machine with gcloud installed and authenticated:
./infra/setup_deploy_iam.sh YOUR_PROJECT_ID us-central1
```
This script:
1. Creates a service account named `d2c-cicd-sa`
2. Grants it `roles/artifactregistry.writer` on the `d2c` repository
3. Creates a JSON key
4. Prints the base64-encoded key to stdout

**Copy the output** (the long base64 string) into the GitHub secret.

**After copying, delete the local key file**:
```bash
rm /tmp/d2c-cicd-sa-key.json
```

**Verify it has the right permissions** (should work):
```bash
cat /tmp/d2c-cicd-sa-key.json | docker login \
  -u _json_key \
  --password-stdin \
  us-central1-docker.pkg.dev
```

---

### UAT_SERVER_HOST
**What**: The IP address or hostname of your UAT server.
**Format**: `203.0.113.10` or `uat.yourdomain.com`
**How to find**: Your VPS provider dashboard, or `gcloud compute instances list`

---

### UAT_SERVER_USER
**What**: The SSH username on the UAT server.
**Default on most VPS providers**: `ubuntu` (Ubuntu), `ec2-user` (AWS), `root`
**Value**: typically `ubuntu`

---

### UAT_SSH_PRIVATE_KEY
**What**: The private half of an SSH key pair that allows login to the UAT server.

**How to generate a dedicated deploy key** (recommended — don't reuse your personal key):
```bash
# On your local machine:
ssh-keygen -t ed25519 -C "d2c-uat-deploy" -f ~/.ssh/d2c_uat_deploy -N ""
# Creates:
#   ~/.ssh/d2c_uat_deploy      ← PRIVATE KEY → goes in GitHub secret
#   ~/.ssh/d2c_uat_deploy.pub  ← PUBLIC KEY  → goes on the server
```

**Add the PUBLIC key to the UAT server**:
```bash
# Copy the public key content:
cat ~/.ssh/d2c_uat_deploy.pub
# Then on the server:
echo "PASTE_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**Add the PRIVATE key to GitHub**:
```bash
cat ~/.ssh/d2c_uat_deploy
# Copy the entire output including -----BEGIN... and -----END... lines
# Paste into the GitHub secret UAT_SSH_PRIVATE_KEY
```

**Test the connection**:
```bash
ssh -i ~/.ssh/d2c_uat_deploy ubuntu@YOUR_UAT_SERVER_IP "echo connected"
# Should print: connected
```

---

## PART 2 — GCP Secret Manager (6 secrets)

These are the application runtime secrets. They are fetched by:
1. The UAT server's `/opt/d2c/.env.uat` (manually, once)
2. Cloud Run production (automatically at startup via secret_manager.py)

All secrets are created by `infra/setup_gcp.sh`. Run it once:
```bash
./infra/setup_gcp.sh YOUR_PROJECT_ID us-central1
```

If you need to check or update a secret value:
```bash
# Read current value:
gcloud secrets versions access latest --secret=d2c-jwt-secret --project=YOUR_PROJECT_ID

# Update value:
echo -n "new_value" | gcloud secrets versions add d2c-jwt-secret --data-file=-
```

### d2c-jwt-secret
**What**: HS256 signing key for JWTs.
**Format**: 64-character hex string
**Generate**: `python3 -c "import secrets; print(secrets.token_hex(32))"`
**Rotation impact**: All existing JWTs become invalid — users must log in again.

### d2c-encryption-key
**What**: Fernet key for encrypting OAuth tokens at rest in the database.
**Format**: 44-character url-safe base64 string (output of Fernet.generate_key())
**Generate**: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
**Rotation**: Use comma-separated keys: `new_key,old_key` — see DEPLOYMENT.md.

### d2c-db-password
**What**: PostgreSQL password for the `d2c_app` user.
**Format**: random URL-safe string
**Generate**: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
**Note**: This password must match what was set when creating the PostgreSQL user.

### d2c-shopify-client-secret
**What**: Shopify app client secret (from Shopify Partners dashboard).
**Where to find**: partners.shopify.com → Your App → API credentials
**For UAT**: Use `mock_shopify_client_secret` (the mock server doesn't verify it)

### d2c-shopify-webhook-secret
**What**: HMAC secret used to verify Shopify webhook signatures.
**Where to find**: partners.shopify.com → Your App → Webhooks → Signing secret
**For UAT**: Use `mock_webhook_hmac_secret`

### d2c-meta-app-secret
**What**: Meta app secret (from developers.facebook.com).
**Where to find**: developers.facebook.com → Your App → Settings → Basic → App Secret
**For UAT**: Use `mock_meta_app_secret`

---

## PART 3 — UAT Server: /opt/d2c/.env.uat

This file lives ONLY on the UAT server. It is never in git.
Created manually, populated from Secret Manager values.

```bash
# On the UAT server:
sudo mkdir -p /opt/d2c
sudo nano /opt/d2c/.env.uat

# Paste content from ansible/.env.uat.template
# Fill in values from Secret Manager (see commands in PART 2)

sudo chmod 600 /opt/d2c/.env.uat     # owner read/write only
sudo chown ubuntu:ubuntu /opt/d2c/.env.uat
```

**Verify it's correct**:
```bash
# Check permissions (should be -rw-------)
ls -la /opt/d2c/.env.uat

# Quick sanity check — should show your values:
grep "ENVIRONMENT\|DATABASE_URL" /opt/d2c/.env.uat
```

---

## PART 4 — Complete Setup Order

Do these steps in order. Each step depends on the previous.

### Step 1 — Local prerequisites
```bash
# Authenticate gcloud
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

### Step 2 — GCP infrastructure
```bash
# Creates: VPC, Cloud SQL, Artifact Registry, app SA, all secrets
./infra/setup_gcp.sh YOUR_PROJECT_ID us-central1
```

### Step 3 — CI/CD service account
```bash
# Creates the deploy SA and prints the base64 key
./infra/setup_deploy_iam.sh YOUR_PROJECT_ID us-central1
# Copy the base64 output → GitHub secret GCP_SA_KEY
```

### Step 4 — SSH key for UAT server
```bash
ssh-keygen -t ed25519 -C "d2c-uat-deploy" -f ~/.ssh/d2c_uat_deploy -N ""
# Copy ~/.ssh/d2c_uat_deploy.pub to the server's authorized_keys
# Copy ~/.ssh/d2c_uat_deploy content → GitHub secret UAT_SSH_PRIVATE_KEY
```

### Step 5 — GitHub Secrets
Add all 7 secrets from PART 1 to your GitHub repository.

### Step 6 — Bootstrap UAT server
```bash
ssh ubuntu@YOUR_UAT_SERVER_IP
./bootstrap_uat_server.sh YOUR_PROJECT_ID us-central1
# Creates Docker network, starts PostgreSQL, installs gcloud
```

### Step 7 — Create .env.uat on server
```bash
# Still on the UAT server:
# Fetch each secret from Secret Manager and paste into .env.uat
gcloud auth activate-service-account --key-file=/path/to/sa-key.json
# (Use the CI/CD SA key temporarily, or your personal gcloud auth)

sudo nano /opt/d2c/.env.uat
# Fill in all values from ansible/.env.uat.template

sudo chmod 600 /opt/d2c/.env.uat
```

### Step 8 — First deploy
```bash
# On your development machine:
git checkout uat
git push origin uat
# Watch GitHub Actions → should build, push, and deploy
```

### Step 9 — Verify
```bash
# From your machine:
curl http://YOUR_UAT_SERVER_IP:8000/health
# Expected: {"status":"healthy","db":"ok","version":"1.0.0"}
```

---

## Troubleshooting

### "Permission denied" pushing to Artifact Registry
The SA key in GCP_SA_KEY may be for the wrong SA, or the SA doesn't have
`roles/artifactregistry.writer`. Check:
```bash
gcloud artifacts repositories get-iam-policy d2c \
  --location=us-central1 \
  --project=YOUR_PROJECT_ID
```

### "Connection refused" when Ansible tries SSH
- The public key wasn't added to `~/.ssh/authorized_keys` on the server
- The wrong user is set in UAT_SERVER_USER
- Port 22 is blocked by a firewall
Test manually: `ssh -i ~/.ssh/d2c_uat_deploy ubuntu@SERVER_IP`

### Migrations fail during deploy
The database container isn't running, or DATABASE_URL in .env.uat is wrong.
On the server:
```bash
docker ps | grep postgres          # is it running?
docker logs d2c-postgres           # any errors?
docker exec -it d2c-postgres psql -U d2c -d d2c_db -c "\dt"
```

### App starts but /health returns "degraded"
The DB is unreachable. Check DATABASE_URL in .env.uat.
The container must be on the d2c-network:
```bash
docker network inspect d2c-network   # both d2c-app and d2c-postgres should appear
```

### "ModuleNotFoundError" or import error on startup
The image was built with an old requirements.txt. Force a rebuild:
```bash
git commit --allow-empty -m "force rebuild" && git push origin uat
```

### Old image still running after deploy
Check Ansible logs in GitHub Actions for the "Start new container" task.
On the server: `docker ps` — what image tag does it show?

### Secret value is wrong at runtime
The .env.uat value doesn't match what's in Secret Manager, or the value
was pasted with extra whitespace. Fetch fresh from Secret Manager:
```bash
gcloud secrets versions access latest --secret=d2c-jwt-secret | cat -A
# $ at end = newline present — strip it with | tr -d '\n'
```