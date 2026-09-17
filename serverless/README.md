# FeatherFlight serverless deployment

Static frontend + AWS Lambda backend for `badminton.nhtnm.com`.

Files:
- `booking_lambda.py` - Python Lambda handler and booking logic.
- `requirements.txt` - Lambda Python dependencies.
- `index.html` - single-page frontend. The deploy script replaces `__API_URL__` with the API Gateway endpoint.
- `deploy_serverless.sh` - creates/updates S3, CloudFront, ACM, Lambda, HTTP API, and Route53 records.

Security notes:
- The frontend sends credentials only in the HTTPS POST body for each booking attempt.
- The Lambda does not store passwords.
- Runtime cache/log files go under `/tmp/featherflight` in Lambda.
