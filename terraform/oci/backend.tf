# Remote state for the OCI root (OpenTofu).
#
# State lives in the in-cluster S3 store (Ceph RGW, namespace object-store),
# bucket tofu-state, under the oci/ prefix. The store is copied to the UNAS
# every hour. Locking uses an S3 lock file.
#
# The endpoint is the in-cluster Service, so this root runs on the in-cluster
# runners. From a workstation on the LAN, override it:
#   tofu init -backend-config='endpoints={s3="https://s3.homelab0.org"}'
#
# Credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY of the tofu-oci user,
# which can only touch keys under oci/.
#
# Encryption: state and plan files are encrypted on the client. The
# configuration comes from the TF_ENCRYPTION environment variable so no key
# material is in Git. Without it OpenTofu cannot read the state.
#
# workspace_key_prefix keeps workspace listings inside the oci/ prefix; the
# default (env:/ at the bucket root) is outside what the user may list.

terraform {
  backend "s3" {
    bucket               = "tofu-state"
    key                  = "oci/terraform.tfstate"
    workspace_key_prefix = "oci/env:"
    region               = "us-east-1"
    use_lockfile         = true

    endpoints = {
      s3 = "http://rgw.object-store.svc.cluster.local"
    }
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
