# Terraform Remote State Backend Configuration for OCI
#
# This configuration stores OCI Terraform state in AWS S3 with S3 native state locking.
# Uses a separate key from the main Kubernetes cattle workflow state.
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
# State Keys:
# - terraform.tfstate        - Main K8s cattle workflow (terraform/)
# - oci/terraform.tfstate    - OCI Plex proxy VM (terraform/oci/)

terraform {
  backend "s3" {
    bucket       = "prox-ops-terraform-state"
    key          = "oci/terraform.tfstate"  # Separate from main K8s state
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true  # S3 native state locking (Terraform 1.10+)
  }
}
