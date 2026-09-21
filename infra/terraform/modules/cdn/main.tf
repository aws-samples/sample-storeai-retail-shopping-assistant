########################################
# CloudFront = the ONLY public ingress (D-031). Serves the static SPA from S3
# and routes /api/*, /images/*, /tryon-images/* to the internal ALB via a
# CloudFront VPC origin. The private ALB is discovered by tag (created by the
# orchestrator Ingress in EKS Auto Mode).
########################################

data "aws_caller_identity" "current" {}

locals {
  frontend_bucket = "storeai-${var.env_name}-frontend-${data.aws_caller_identity.current.account_id}"

  # The app is served at a dedicated subdomain (storeai.<custom_domain>), never the
  # bare domain — users bring existing domains that may already serve other content.
  enable_custom_domain = var.custom_domain != "" && var.hosted_zone_id != ""
  app_domain           = local.enable_custom_domain ? "storeai.${var.custom_domain}" : ""
}

########################################
# Custom-domain wiring (optional): us-east-1 ACM cert for storeai.<domain>,
# a CloudFront alias, and a Route53 CNAME -> the distribution. All gated on
# enable_custom_domain, so a no-domain deploy is unaffected (default CF cert).
########################################
resource "aws_acm_certificate" "app" {
  count             = local.enable_custom_domain ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = local.app_domain
  validation_method = "DNS"
  tags              = var.tags
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "app_cert_validation" {
  for_each = local.enable_custom_domain ? {
    for dvo in aws_acm_certificate.app[0].domain_validation_options : dvo.domain_name => {
      name = dvo.resource_record_name, type = dvo.resource_record_type, record = dvo.resource_record_value
    }
  } : {}
  zone_id         = var.hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "app" {
  count                   = local.enable_custom_domain ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.app[0].arn
  validation_record_fqdns = [for r in aws_route53_record.app_cert_validation : r.fqdn]
}

# storeai.<domain> -> CloudFront (CNAME; a subdomain, so no apex-alias needed).
resource "aws_route53_record" "app" {
  count   = local.enable_custom_domain ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = local.app_domain
  type    = "CNAME"
  ttl     = 300
  records = [aws_cloudfront_distribution.main.domain_name]
}

########################################
# Frontend S3 bucket (private, OAC-only access)
########################################

resource "aws_s3_bucket" "frontend" {
  bucket        = local.frontend_bucket
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Static export (rebuilt on deploy) — no object expiry, but abort stale multipart uploads (CKV2_AWS_61).
resource "aws_s3_bucket_lifecycle_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
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
# Origin Access Control + CloudFront Functions
########################################

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "storeai-${var.env_name}-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# SPA URL rewrite for the static Next.js export (dir → index.html, extensionless → .html)
resource "aws_cloudfront_function" "spa" {
  name    = "storeai-${var.env_name}-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      if (uri.endsWith('/')) { request.uri += 'index.html'; }
      else if (!uri.includes('.')) { request.uri += '.html'; }
      return request;
    }
  EOT
}

# Strip the /api prefix so the orchestrator sees its native routes (/chat, /search_products, ...)
resource "aws_cloudfront_function" "strip_api" {
  name    = "storeai-${var.env_name}-strip-api"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      // Edge auth gate (defense-in-depth): /api/* requires a bearer token.
      // Real JWT verification happens at the application. /api/health stays open.
      // /api/tryon-share is a public single-use QR gallery (scanned on a phone with
      // no token; the token in the query string is validated at the application).
      if (uri.startsWith('/api/') && uri !== '/api/health' && uri !== '/api/tryon-share') {
        if (!request.headers.authorization) {
          return {
            statusCode: 401,
            statusDescription: 'Unauthorized',
            headers: { 'content-type': { value: 'application/json' } },
            body: '{"detail":"missing bearer token (edge)"}'
          };
        }
      }
      if (uri.startsWith('/api/')) { request.uri = uri.substring(4); }
      else if (uri === '/api') { request.uri = '/'; }
      return request;
    }
  EOT
}

########################################
# CloudFront VPC origin → internal ALB (HTTP:80)
########################################

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = "storeai-${var.env_name}-alb"
    arn                    = var.alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
  tags = var.tags
}

########################################
# Managed cache / origin-request policies
########################################

data "aws_cloudfront_cache_policy" "optimized" { name = "Managed-CachingOptimized" }
data "aws_cloudfront_cache_policy" "disabled" { name = "Managed-CachingDisabled" }
data "aws_cloudfront_origin_request_policy" "all_viewer" { name = "Managed-AllViewer" }
data "aws_cloudfront_response_headers_policy" "security" { name = "Managed-SecurityHeadersPolicy" }

########################################
# Distribution
########################################

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "storeai-${var.env_name}"
  default_root_object = "index.html"
  price_class         = var.price_class
  tags                = var.tags
  aliases             = local.enable_custom_domain ? [local.app_domain] : []

  origin {
    origin_id                = "s3-frontend"
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    origin_id   = "alb-api"
    domain_name = var.alb_dns_name
    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.alb.id
      origin_read_timeout      = 60 # Qwen Image Edit VTON ~36s > default 30s; 60s is the max without a quota increase
      origin_keepalive_timeout = 5
    }
  }

  # Default → static SPA from S3
  default_cache_behavior {
    target_origin_id           = "s3-frontend"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
    compress                   = true
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa.arn
    }
  }

  # /api/* → ALB (strip prefix, no caching, forward everything)
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.strip_api.arn
    }
  }

  # Product images (orchestrator 302-redirects to presigned S3 → do not cache)
  ordered_cache_behavior {
    path_pattern             = "/images/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  # Try-on result images (orchestrator streams bytes)
  ordered_cache_behavior {
    path_pattern             = "/tryon-images/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  # Try-on result images referenced by their raw S3 key prefix (/tryon-results/*).
  # Aliased to the same orchestrator proxy route as /tryon-images/* so either form works.
  ordered_cache_behavior {
    path_pattern             = "/tryon-results/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  # Voice WebSockets (Nova Sonic STT/TTS) → ALB → voice-nova. WS needs no caching
  # and AllViewer (forwards Sec-WebSocket-* + Upgrade/Connection headers).
  ordered_cache_behavior {
    path_pattern             = "/stt"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  ordered_cache_behavior {
    path_pattern             = "/tts"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  ordered_cache_behavior {
    path_pattern             = "/whisper"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  # Avatar HTML and auth endpoints share the frontend origin; never cache credentials.
  dynamic "ordered_cache_behavior" {
    for_each = ["/avatar", "/avatars", "/token", "/pair"]
    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = "alb-api"
      viewer_protocol_policy   = "https-only"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
      compress                 = false
    }
  }

  ordered_cache_behavior {
    path_pattern             = "/ws"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = local.enable_custom_domain ? null : true
    acm_certificate_arn            = local.enable_custom_domain ? aws_acm_certificate_validation.app[0].certificate_arn : null
    ssl_support_method             = local.enable_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.enable_custom_domain ? "TLSv1.2_2021" : null
  }
}

########################################
# S3 bucket policy — allow only this CloudFront distribution via OAC
########################################

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.main.arn } }
    }]
  })
}
