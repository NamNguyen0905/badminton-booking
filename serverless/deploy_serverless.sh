#!/usr/bin/env bash
set -euo pipefail

REGION=${REGION:-us-west-2}
CERT_REGION=us-east-1
DOMAIN=${DOMAIN:-badminton.nhtnm.com}
ROOT_DOMAIN=${ROOT_DOMAIN:-nhtnm.com}
HOSTED_ZONE_ID=${HOSTED_ZONE_ID:-Z05671803NIK47FX1V5UQ}
LAMBDA_NAME=${LAMBDA_NAME:-featherflight-booking}
METRICS_LAMBDA_NAME=${METRICS_LAMBDA_NAME:-featherflight-metrics}
ROLE_NAME=${ROLE_NAME:-lambda-featherflight-booking-role}
API_NAME=${API_NAME:-featherflight-booking-api}
BUCKET=${BUCKET:-nhtnm-badminton-web-$(aws sts get-caller-identity --query Account --output text)-${REGION}}
EVENTS_BUCKET=${EVENTS_BUCKET:-nhtnm-badminton-events-$(aws sts get-caller-identity --query Account --output text)-${REGION}}
METRICS_PASSWORD=${METRICS_PASSWORD:-}
CF_ZONE_ID=Z2FDTNDATAQYW2

# Preserve the existing dashboard password unless a new one is explicitly provided.
# This avoids accidentally disabling /dashboard.html during routine deploys.
if [ -z "$METRICS_PASSWORD" ]; then
  EXISTING_METRICS_PASSWORD=$(aws lambda get-function-configuration \
    --function-name "$METRICS_LAMBDA_NAME" \
    --region "$REGION" \
    --query 'Environment.Variables.METRICS_PASSWORD' \
    --output text 2>/dev/null || true)
  if [ "$EXISTING_METRICS_PASSWORD" != "None" ] && [ -n "$EXISTING_METRICS_PASSWORD" ]; then
    METRICS_PASSWORD="$EXISTING_METRICS_PASSWORD"
    echo "Preserving existing metrics dashboard password."
  else
    echo "ERROR: METRICS_PASSWORD is required because no existing metrics password is configured." >&2
    echo "Run: METRICS_PASSWORD='***' ./deploy_serverless.sh" >&2
    exit 1
  fi
fi
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
BUILD_DIR="$BASE_DIR/.build"
PACKAGE_DIR="$BUILD_DIR/package"
ZIP_PATH="$BUILD_DIR/lambda.zip"

echo "Deploying $DOMAIN in AWS account $(aws sts get-caller-identity --query Account --output text), app region $REGION"

# Lambda execution role
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  tmp=$(mktemp)
  cat > "$tmp" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$tmp" >/dev/null
  rm "$tmp"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null 2>&1 || true
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

# Events S3 bucket (private — only Lambda reads/writes)
if ! aws s3api head-bucket --bucket "$EVENTS_BUCKET" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$EVENTS_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$EVENTS_BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "$EVENTS_BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
echo "Events bucket: $EVENTS_BUCKET"

# Grant Lambda role S3 read/write on events bucket
S3_POLICY_NAME=lambda-featherflight-s3-events
S3_POLICY_DOC=$(cat <<S3JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::${EVENTS_BUCKET}","arn:aws:s3:::${EVENTS_BUCKET}/*"]}]}
S3JSON
)
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "$S3_POLICY_NAME" --policy-document "$S3_POLICY_DOC" >/dev/null 2>&1
echo "Attached S3 events policy to $ROLE_NAME"

# Package Lambda zip. Dependencies are pure Python, so local build is acceptable.
rm -rf "$BUILD_DIR"
mkdir -p "$PACKAGE_DIR"
python3 -m pip install --quiet --target "$PACKAGE_DIR" -r "$BASE_DIR/requirements.txt"
cp "$BASE_DIR/booking_lambda.py" "$PACKAGE_DIR/"
(cd "$PACKAGE_DIR" && zip -qr "$ZIP_PATH" .)

# Create/update Lambda
if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$LAMBDA_NAME" --zip-file "fileb://$ZIP_PATH" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"
  aws lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.11 \
    --handler booking_lambda.lambda_handler \
    --timeout 60 \
    --memory-size 512 \
    --environment "Variables={ALLOW_ORIGIN=https://$DOMAIN,FEATHERFLIGHT_RUNTIME_DIR=/tmp/featherflight,EVENTS_S3_BUCKET=$EVENTS_BUCKET,EVENTS_S3_PREFIX=metrics/events/}" \
    --region "$REGION" >/dev/null
else
  # Give new IAM role a moment to propagate.
  sleep 10
  aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.11 \
    --role "$ROLE_ARN" \
    --handler booking_lambda.lambda_handler \
    --zip-file "fileb://$ZIP_PATH" \
    --timeout 60 \
    --memory-size 512 \
    --environment "Variables={ALLOW_ORIGIN=https://$DOMAIN,FEATHERFLIGHT_RUNTIME_DIR=/tmp/featherflight,EVENTS_S3_BUCKET=$EVENTS_BUCKET,EVENTS_S3_PREFIX=metrics/events/}" \
    --region "$REGION" >/dev/null
fi
aws lambda wait function-active --function-name "$LAMBDA_NAME" --region "$REGION"
LAMBDA_ARN=$(aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)
echo "Lambda: $LAMBDA_ARN"

# Metrics Lambda
METRICS_ZIP_PATH="$BUILD_DIR/metrics_lambda.zip"
cp "$BASE_DIR/metrics_lambda.py" "$PACKAGE_DIR/"
(cd "$PACKAGE_DIR" && zip -qr "$METRICS_ZIP_PATH" metrics_lambda.py booking_lambda.py)
METRICS_ENV_JSON=$(mktemp)
python3 - "$DOMAIN" "$EVENTS_BUCKET" "$METRICS_PASSWORD" > "$METRICS_ENV_JSON" <<'PY'
import json
import sys

domain, events_bucket, metrics_password = sys.argv[1:4]
print(json.dumps({
    "Variables": {
        "ALLOW_ORIGIN": f"https://{domain}",
        "EVENTS_S3_BUCKET": events_bucket,
        "EVENTS_S3_PREFIX": "metrics/events/",
        "METRICS_PASSWORD": metrics_password,
    }
}))
PY
if aws lambda get-function --function-name "$METRICS_LAMBDA_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$METRICS_LAMBDA_NAME" --zip-file "fileb://$METRICS_ZIP_PATH" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$METRICS_LAMBDA_NAME" --region "$REGION"
  aws lambda update-function-configuration \
    --function-name "$METRICS_LAMBDA_NAME" \
    --runtime python3.11 \
    --handler metrics_lambda.lambda_handler \
    --timeout 30 \
    --memory-size 256 \
    --environment "file://$METRICS_ENV_JSON" \
    --region "$REGION" >/dev/null
else
  aws lambda create-function \
    --function-name "$METRICS_LAMBDA_NAME" \
    --runtime python3.11 \
    --role "$ROLE_ARN" \
    --handler metrics_lambda.lambda_handler \
    --zip-file "fileb://$METRICS_ZIP_PATH" \
    --timeout 30 \
    --memory-size 256 \
    --environment "file://$METRICS_ENV_JSON" \
    --region "$REGION" >/dev/null
fi
aws lambda wait function-active --function-name "$METRICS_LAMBDA_NAME" --region "$REGION"
METRICS_LAMBDA_ARN=$(aws lambda get-function --function-name "$METRICS_LAMBDA_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)
echo "Metrics Lambda: $METRICS_LAMBDA_ARN"

# HTTP API Gateway
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 create-api \
    --name "$API_NAME" \
    --protocol-type HTTP \
    --cors-configuration "AllowOrigins=https://$DOMAIN,AllowMethods=POST,GET,OPTIONS,AllowHeaders=content-type,x-booking-source,MaxAge=300" \
    --region "$REGION" \
    --query 'ApiId' --output text)
else
  aws apigatewayv2 update-api \
    --api-id "$API_ID" \
    --cors-configuration "AllowOrigins=https://$DOMAIN,AllowMethods=POST,GET,OPTIONS,AllowHeaders=content-type,x-booking-source,MaxAge=300" \
    --region "$REGION" >/dev/null
fi
INTEGRATION_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations"
INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" --query "Items[?IntegrationUri=='$INTEGRATION_URI'].IntegrationId | [0]" --output text)
if [ "$INTEGRATION_ID" = "None" ] || [ -z "$INTEGRATION_ID" ]; then
  INTEGRATION_ID=$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$INTEGRATION_URI" \
    --payload-format-version 2.0 \
    --region "$REGION" \
    --query 'IntegrationId' --output text)
fi
for ROUTE_KEY in 'POST /book' 'GET /health'; do
  if ! aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" --query "Items[?RouteKey=='$ROUTE_KEY'].RouteId | [0]" --output text | grep -vq '^None$'; then
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$ROUTE_KEY" --target "integrations/$INTEGRATION_ID" --region "$REGION" >/dev/null
  fi
done
if ! aws apigatewayv2 get-stages --api-id "$API_ID" --region "$REGION" --query "Items[?StageName=='\$default'].StageName | [0]" --output text | grep -vq '^None$'; then
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy --region "$REGION" >/dev/null
fi
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id apigatewayv2-featherflight-booking \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:$(aws sts get-caller-identity --query Account --output text):${API_ID}/*/*/*" \
  --region "$REGION" >/dev/null 2>&1 || true
API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/book"
echo "API: $API_URL"

# Metrics Lambda integration + route
METRICS_INTEGRATION_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${METRICS_LAMBDA_ARN}/invocations"
METRICS_INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" --query "Items[?IntegrationUri=='$METRICS_INTEGRATION_URI'].IntegrationId | [0]" --output text)
if [ "$METRICS_INTEGRATION_ID" = "None" ] || [ -z "$METRICS_INTEGRATION_ID" ]; then
  METRICS_INTEGRATION_ID=$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$METRICS_INTEGRATION_URI" \
    --payload-format-version 2.0 \
    --region "$REGION" \
    --query 'IntegrationId' --output text)
fi
if ! aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" --query "Items[?RouteKey=='GET /metrics'].RouteId | [0]" --output text | grep -vq '^None$'; then
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key 'GET /metrics' --target "integrations/$METRICS_INTEGRATION_ID" --region "$REGION" >/dev/null
fi
aws lambda add-permission \
  --function-name "$METRICS_LAMBDA_NAME" \
  --statement-id apigatewayv2-featherflight-metrics \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:$(aws sts get-caller-identity --query Account --output text):${API_ID}/*/*/*" \
  --region "$REGION" >/dev/null 2>&1 || true
METRICS_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/metrics"
echo "Metrics API: $METRICS_URL"

# S3 static site bucket
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false >/dev/null
aws s3api put-bucket-website --bucket "$BUCKET" --website-configuration '{"IndexDocument":{"Suffix":"index.html"},"ErrorDocument":{"Key":"index.html"}}' >/dev/null
tmp_policy=$(mktemp)
cat > "$tmp_policy" <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"PublicReadStaticSite","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::$BUCKET/*"}]}
EOF
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$tmp_policy" >/dev/null
rm "$tmp_policy"

FRONTEND_BUILD="$BUILD_DIR/site"
mkdir -p "$FRONTEND_BUILD"
sed "s#__API_URL__#$API_URL#g" "$BASE_DIR/index.html" > "$FRONTEND_BUILD/index.html"
sed "s#__METRICS_URL__#$METRICS_URL#g" "$BASE_DIR/dashboard.html" > "$FRONTEND_BUILD/dashboard.html"
aws s3 sync "$FRONTEND_BUILD/" "s3://$BUCKET/" --delete --content-type text/html --cache-control 'no-cache, no-store, must-revalidate' >/dev/null
S3_WEBSITE_DOMAIN="${BUCKET}.s3-website-${REGION}.amazonaws.com"
echo "S3 website origin: $S3_WEBSITE_DOMAIN"

# ACM cert for CloudFront in us-east-1
CERT_ARN=$(aws acm list-certificates --region "$CERT_REGION" --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
if [ "$CERT_ARN" = "None" ] || [ -z "$CERT_ARN" ]; then
  CERT_ARN=$(aws acm request-certificate --domain-name "$DOMAIN" --validation-method DNS --region "$CERT_REGION" --query CertificateArn --output text)
fi
echo "Certificate: $CERT_ARN"
# Add DNS validation records
for i in {1..20}; do
  VALIDATION_JSON=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$CERT_REGION" --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output json)
  if [ "$VALIDATION_JSON" != "null" ]; then break; fi
  sleep 5
done
RR_NAME=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Name"])' <<< "$VALIDATION_JSON")
RR_VALUE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Value"])' <<< "$VALIDATION_JSON")
tmp_rr=$(mktemp)
cat > "$tmp_rr" <<EOF
{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"$RR_NAME","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"$RR_VALUE"}]}}]}
EOF
aws route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" --change-batch "file://$tmp_rr" >/dev/null
rm "$tmp_rr"
echo "Waiting for ACM validation..."
aws acm wait certificate-validated --certificate-arn "$CERT_ARN" --region "$CERT_REGION"

# CloudFront distribution
DIST_ID=$(aws cloudfront list-distributions --query "DistributionList.Items[?Aliases.Items && contains(Aliases.Items, '$DOMAIN')].Id | [0]" --output text)
if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  CALLER="featherflight-${DOMAIN}-$(date +%s)"
  tmp_cf=$(mktemp)
  cat > "$tmp_cf" <<EOF
{
  "CallerReference":"$CALLER",
  "Aliases":{"Quantity":1,"Items":["$DOMAIN"]},
  "DefaultRootObject":"index.html",
  "Origins":{"Quantity":1,"Items":[{"Id":"s3-website","DomainName":"$S3_WEBSITE_DOMAIN","CustomOriginConfig":{"HTTPPort":80,"HTTPSPort":443,"OriginProtocolPolicy":"http-only","OriginSslProtocols":{"Quantity":1,"Items":["TLSv1.2"]},"OriginReadTimeout":30,"OriginKeepaliveTimeout":5}}]},
  "DefaultCacheBehavior":{"TargetOriginId":"s3-website","ViewerProtocolPolicy":"redirect-to-https","AllowedMethods":{"Quantity":2,"Items":["GET","HEAD"],"CachedMethods":{"Quantity":2,"Items":["GET","HEAD"]}},"Compress":true,"ForwardedValues":{"QueryString":false,"Cookies":{"Forward":"none"}},"MinTTL":0,"DefaultTTL":3600,"MaxTTL":86400},
  "Comment":"FeatherFlight booking static frontend",
  "Enabled":true,
  "ViewerCertificate":{"ACMCertificateArn":"$CERT_ARN","SSLSupportMethod":"sni-only","MinimumProtocolVersion":"TLSv1.2_2021"}
}
EOF
  DIST_ID=$(aws cloudfront create-distribution --distribution-config "file://$tmp_cf" --query 'Distribution.Id' --output text)
  rm "$tmp_cf"
else
  echo "Using existing CloudFront distribution $DIST_ID"
fi
CF_DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DomainName' --output text)
echo "CloudFront: $DIST_ID $CF_DOMAIN"

# Route53 alias to CloudFront
tmp_dns=$(mktemp)
cat > "$tmp_dns" <<EOF
{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"$DOMAIN","Type":"A","AliasTarget":{"HostedZoneId":"$CF_ZONE_ID","DNSName":"$CF_DOMAIN","EvaluateTargetHealth":false}}}]}
EOF
aws route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" --change-batch "file://$tmp_dns" >/dev/null
rm "$tmp_dns"

# Invalidate if distribution already deployed enough to accept invalidation.
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths '/*' >/dev/null || true

echo "Waiting for CloudFront deployment. This can take several minutes..."
aws cloudfront wait distribution-deployed --id "$DIST_ID"

echo "DONE"
echo "Frontend: https://$DOMAIN"
echo "Dashboard: https://$DOMAIN/dashboard.html"
echo "API health: https://${API_ID}.execute-api.${REGION}.amazonaws.com/health"
echo "Lambda: $LAMBDA_NAME"
echo "S3 bucket: $BUCKET"
echo "CloudFront distribution: $DIST_ID"
