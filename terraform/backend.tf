# Terraform Remote State Backend Configuration
#
# This configuration stores Terraform state in AWS S3 with DynamoDB state locking.
# This enables team collaboration and prevents concurrent state modifications.
#
# Prerequisites:
# - AWS credentials configured (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
# - S3 bucket: prox-ops-terraform-state (created in us-east-2)
# - DynamoDB table: prox-ops-terraform-locks (created in us-east-2)
#
# For GitHub Actions, configure these secrets:
# - AWS_ACCESS_KEY_ID
# - AWS_SECRET_ACCESS_KEY
# - AWS_DEFAULT_REGION (us-east-2)

terraform {
  backend "s3" {
    bucket         = "prox-ops-terraform-state"
    key            = "terraform.tfstate"
    region         = "us-east-2"
    encrypt        = true
    dynamodb_table = "prox-ops-terraform-locks"

    # State locking prevents concurrent modifications
    # DynamoDB table uses LockID as partition key
  }
}
