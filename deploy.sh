#!/usr/bin/env bash
# Baut das Docker-Image und pusht es nach AWS ECR.
# Voraussetzung: AWS-CLI installiert + `aws configure` gemacht, Docker läuft.
#
# Nutzung:   AWS_REGION=eu-central-1 ./deploy.sh
set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
REPO="${ECR_REPO:-powerbi-generator}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
IMAGE="$REGISTRY/$REPO:latest"

echo "==> ECR-Repo sicherstellen ($REPO in $REGION)"
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION" >/dev/null

echo "==> Docker bei ECR anmelden"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Image bauen, taggen, pushen"
docker build -t "$REPO" .
docker tag "$REPO:latest" "$IMAGE"
docker push "$IMAGE"

echo ""
echo "Fertig ✅  Image: $IMAGE"
echo "Diese Image-URI in App Runner als Quelle angeben (bzw. 'Deploy' klicken)."
