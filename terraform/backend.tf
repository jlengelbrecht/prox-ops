# Terraform Remote State Backend Configuration
#
# This configuration stores Terraform state in AWS S3 with S3 native state locking.
# Migration from DynamoDB locking to S3 native locking in progress.
#
# Prerequisites:
# - AWS credentials configured (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
# - S3 bucket: prox-ops-terraform-state (created in us-east-2)
# - DynamoDB table: prox-ops-terraform-locks (kept during migration)
#
# For GitHub Actions, configure these secrets:
# - AWS_ACCESS_KEY_ID
# - AWS_SECRET_ACCESS_KEY
# - AWS_DEFAULT_REGION (us-east-2)
#
# Migration Status:
# - Phase 1: Dual locking (S3 native + DynamoDB) - CURRENT
# - Phase 2: S3 native only (remove dynamodb_table after testing)

terraform {
  backend "s3" {
    bucket         = "prox-ops-terraform-state"
    key            = "terraform.tfstate"
    region         = "us-east-2"
    encrypt        = true
    use_lockfile   = true                        # S3 native state locking (Terraform 1.10+)
    dynamodb_table = "prox-ops-terraform-locks"  # Kept during migration for safety

    # State locking using S3 conditional writes
    # DynamoDB locking maintained during transition phase
  }
}
