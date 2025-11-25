# Terraform Remote State Backend Configuration
#
# This configuration stores Terraform state in AWS S3 with S3 native state locking.
# Migration from DynamoDB locking to S3 native locking completed.
#
# Prerequisites:
# - AWS credentials configured (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
# - S3 bucket: prox-ops-terraform-state (created in us-east-2)
#
# For GitHub Actions, configure these secrets:
# - AWS_ACCESS_KEY_ID
# - AWS_SECRET_ACCESS_KEY
# - AWS_DEFAULT_REGION (us-east-2)
#
# Migration Status:
# - Phase 1: Dual locking (S3 native + DynamoDB) - COMPLETED
# - Phase 2: S3 native only (deprecated dynamodb_table removed) - CURRENT

terraform {
  backend "s3" {
    bucket       = "prox-ops-terraform-state"
    key          = "terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true  # S3 native state locking (Terraform 1.10+)

    # State locking using S3 conditional writes
    # DynamoDB no longer required (Terraform 1.10+ native locking)
  }
}
