#!/bin/bash
export AWS_REGION=eu-west-2
export AWS_DEFAULT_REGION=eu-west-2
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

# S3 buckets — names mirror the real AWS resources referenced by the
# per-service .env files so the same config works against LocalStack.
aws --endpoint-url=http://localhost:4566 s3 mb s3://pocldnaia001
echo "Created S3 bucket: pocldnaia001"

# SQS queues
aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name pocldnaia-tasks
echo "Created SQS queue: pocldnaia-tasks"
aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name pocldnaia-status
echo "Created SQS queue: pocldnaia-status"