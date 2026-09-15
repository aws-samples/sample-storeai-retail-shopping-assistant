########################################
# Data Plane — DynamoDB, S3, Cognito, SSM
########################################

data "aws_caller_identity" "current" {}

locals {
  prefix = "storeai-${var.env_name}"

  # DynamoDB table definitions
  tables = {
    products = {
      hash_key  = "product_id"
      range_key = null
      pitr      = false
      ttl       = null
      gsis = {
        "category-index" = { hash_key = "category", range_key = "product_id" }
        "brand-index"    = { hash_key = "brand", range_key = "product_id" }
      }
    }
    customers = {
      hash_key  = "customer_id"
      range_key = null
      pitr      = true
      ttl       = null
      gsis = {
        "email-index" = { hash_key = "email", range_key = null }
        "phone-index" = { hash_key = "phone", range_key = null }
      }
    }
    carts = {
      hash_key  = "customer_id"
      range_key = "cart_item_id"
      pitr      = false
      ttl       = null
      gsis      = {}
    }
    orders = {
      hash_key  = "order_id"
      range_key = null
      pitr      = true
      ttl       = null
      gsis = {
        "customer-index"            = { hash_key = "customer_id", range_key = "order_id" }
        "customer-order-date-index" = { hash_key = "customer_id", range_key = "created_at" }
      }
    }
    tryon-room = {
      hash_key  = "customer_id"
      range_key = "product_id"
      pitr      = false
      ttl       = null
      gsis      = {}
    }
    memory = {
      hash_key  = "customer_id"
      range_key = "created_at"
      pitr      = false
      ttl       = null
      gsis      = {}
    }
    sessions = {
      hash_key  = "session_id"
      range_key = null
      pitr      = false
      ttl       = "ttl"
      gsis      = {}
    }
    checkpoints = {
      hash_key  = "thread_id"
      range_key = "checkpoint_id"
      pitr      = false
      ttl       = null
      gsis      = {}
    }
    share-tokens = {
      hash_key  = "token"
      range_key = null
      pitr      = false
      ttl       = "ttl"
      gsis      = {}
    }
  }

  tags = {
    Project     = "StoreAI"
    Environment = var.env_name
    ManagedBy   = "terraform"
  }
}

########################################
# DynamoDB Tables
########################################

resource "aws_dynamodb_table" "tables" {
  for_each = local.tables

  name         = "${local.prefix}-${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash_key
  range_key    = each.value.range_key

  attribute {
    name = each.value.hash_key
    type = "S"
  }

  dynamic "attribute" {
    for_each = each.value.range_key != null ? [each.value.range_key] : []
    content {
      name = attribute.value
      type = "S"
    }
  }

  # GSI key attributes
  # GSI key attributes (hash + range), deduped — supports multiple GSIs that
  # share a hash key (e.g. orders customer-index + customer-order-date-index).
  dynamic "attribute" {
    for_each = toset([
      for k in concat(
        [for g in values(each.value.gsis) : g.hash_key],
        [for g in values(each.value.gsis) : g.range_key if g.range_key != null]
      ) : k
      if k != each.value.hash_key && (each.value.range_key == null || k != each.value.range_key)
    ])
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "global_secondary_index" {
    for_each = each.value.gsis
    content {
      name            = global_secondary_index.key
      hash_key        = global_secondary_index.value.hash_key
      range_key       = global_secondary_index.value.range_key
      projection_type = "ALL"
    }
  }

  dynamic "ttl" {
    for_each = each.value.ttl != null ? [each.value.ttl] : []
    content {
      attribute_name = ttl.value
      enabled        = true
    }
  }

  point_in_time_recovery {
    enabled = each.value.pitr
  }

  tags = local.tags
}

########################################
# S3 Buckets
########################################

resource "aws_s3_bucket" "tryon" {
  bucket        = "${local.prefix}-tryon-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tryon" {
  bucket = aws_s3_bucket.tryon.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "tryon" {
  bucket = aws_s3_bucket.tryon.id
  rule {
    id     = "expire-tryon-results"
    status = "Enabled"
    filter {
      prefix = "tryon-results/"
    }
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  # Source customer photos, uploaded at photos/<customer_id>/photo.png. These are
  # the personal input the try-on results are generated from, so they should not
  # outlive the results that expire above. Same 7 days.
  rule {
    id     = "expire-tryon-photos"
    status = "Enabled"
    filter {
      prefix = "photos/"
    }
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  # Bucket-wide abort of incomplete multipart uploads (CKV_AWS_300): the rule
  # above is prefix-scoped, so Checkov wants an unscoped rule that guarantees
  # stale multipart uploads are cleaned up anywhere in the bucket.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket" "product_images" {
  bucket        = "${local.prefix}-product-images-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "product_images" {
  bucket = aws_s3_bucket.product_images.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access on both data buckets (CKV2_AWS_6). Access is via
# CloudFront/OAC, the orchestrator (IAM), or short-lived presigned URLs — never public.
resource "aws_s3_bucket_public_access_block" "tryon" {
  bucket                  = aws_s3_bucket.tryon.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "product_images" {
  bucket                  = aws_s3_bucket.product_images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# product-images has no expiry (it's the catalog), but abort stale multipart
# uploads (CKV2_AWS_61, CKV_AWS_300).
resource "aws_s3_bucket_lifecycle_configuration" "product_images" {
  bucket = aws_s3_bucket.product_images.id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

########################################
# Cognito
########################################

resource "aws_cognito_user_pool" "main" {
  name = "${local.prefix}-users"

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  # Admin sign-in users are provisioned by the deployer (deploy/modules/cognito-user.sh),
  # never self-signup. A real admin email gets an emailed one-time invite; the invitee sets
  # their own password on first sign-in. No password is ever stored in config.
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "Your StoreAI sign-in"
      email_message = "You have been invited to StoreAI. Username: {username} Temporary password: {####}. Sign in and you will be prompted to set your own password."
      # Cognito validates all channels when a template is set, so an SMS template is required
      # even though invites are delivered by email. Must include {username} and {####}.
      sms_message = "StoreAI sign-in — username {username}, temporary password {####}"
    }
  }

  # Cognito's built-in email delivers the invitation (no SES required for a demo; ~50/day cap).
  # For production, switch to email_sending_account = "DEVELOPER" with an SES source_arn.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  tags = local.tags
}

resource "aws_cognito_user_pool_client" "main" {
  name                = "${local.prefix}-client"
  user_pool_id        = aws_cognito_user_pool.main.id
  generate_secret     = false
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
}

########################################
# SSM Parameters
########################################

resource "aws_ssm_parameter" "table_names" {
  for_each = aws_dynamodb_table.tables

  name  = "/${local.prefix}/tables/${each.key}"
  type  = "String"
  value = each.value.name
  tags  = local.tags
}

resource "aws_ssm_parameter" "tryon_bucket" {
  name  = "/${local.prefix}/buckets/tryon"
  type  = "String"
  value = aws_s3_bucket.tryon.id
  tags  = local.tags
}

resource "aws_ssm_parameter" "images_bucket" {
  name  = "/${local.prefix}/buckets/product-images"
  type  = "String"
  value = aws_s3_bucket.product_images.id
  tags  = local.tags
}
