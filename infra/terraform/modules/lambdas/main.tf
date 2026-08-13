########################################
# MCP Lambdas (ported from CFN → Terraform, D-013/D-027)
#   - Native packaging via archive_file (no deploy bucket — fixes #2)
#   - Table names injected directly from data-plane outputs (no SSM
#     /app/storeai/... indirection — fixes #1/#3)
#   - VPC-attached (private subnets); reach AWS via VPC endpoints + NAT (D-019)
########################################

locals {
  prefix = "storeai-${var.env_name}"
  n      = var.table_names

  # Per-function config: timeout + environment.
  functions = {
    "catalog-mcp"  = { timeout = 30, env = { PRODUCT_TABLE = local.n["products"] } }
    "customer-mcp" = { timeout = 30, env = { CUSTOMER_TABLE = local.n["customers"], ORDER_TABLE = local.n["orders"] } }
    "cart-mcp"     = { timeout = 30, env = { CART_TABLE = local.n["carts"], TRYON_ROOM_TABLE = local.n["tryon-room"], ORDER_TABLE = local.n["orders"], PRODUCT_TABLE = local.n["products"] } }
    "tryon-mcp"    = { timeout = 60, env = merge({ PRODUCT_TABLE = local.n["products"], TRYON_BUCKET = var.tryon_bucket, PRODUCT_IMAGES_BUCKET = var.product_images_bucket, TRYON_JOB_TABLE = local.n["tryon-room"] }, var.vton_alb_url != "" ? { QWEN_VTON_URL = "${var.vton_alb_url}/vton", FASHN_VTON_URL = "${var.vton_alb_url}/vton-fashn" } : {}) } # Neuron VTON via internal ALB (D-040)
    "memory-mcp"   = { timeout = 60, env = { MEMORY_TABLE = local.n["memory"], VECTOR_BUCKET = var.vector_bucket_name, MEMORY_INDEX = var.memory_index_name } }
    "size-rec-mcp" = { timeout = 30, env = { CUSTOMER_TABLE = local.n["customers"], PRODUCT_TABLE = local.n["products"], TRYON_BUCKET = var.tryon_bucket } }
  }

  tags = { Project = "StoreAI", Environment = var.env_name, ManagedBy = "terraform" }
}

########################################
# IAM role (shared by all MCP functions)
########################################

resource "aws_iam_role" "mcp" {
  name = "${local.prefix}-mcp-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.mcp.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.mcp.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "mcp" {
  name = "mcp-access"
  role = aws_iam_role.mcp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem", "dynamodb:UpdateItem"]
        Resource = "arn:aws:dynamodb:${var.region}:*:table/storeai-*"
      },
      { Sid = "S3", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:HeadObject"], Resource = ["arn:aws:s3:::storeai-${var.env_name}-*", "arn:aws:s3:::storeai-${var.env_name}-*/*"] },
      # Least-privilege: memory-mcp invokes Titan Text Embeddings v2 (vector embeddings) and
      # Claude Haiku (memory summarization, via the us. cross-region inference profile). Region
      # wildcard on the foundation models covers all inference-profile destination Regions.
      { Sid = "Bedrock", Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:${var.region}:*:inference-profile/*.anthropic.claude-*",
      ] },
      {
        Sid      = "S3Vectors"
        Effect   = "Allow"
        Action   = ["s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:ListVectors", "s3vectors:GetIndex", "s3vectors:GetVectorBucket"]
        Resource = ["arn:aws:s3vectors:${var.region}:*:bucket/storeai-${var.env_name}-mem-vectors", "arn:aws:s3vectors:${var.region}:*:bucket/storeai-${var.env_name}-mem-vectors/index/*"]
      },
      { Sid = "LambdaInvoke", Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:${var.region}:*:function:storeai-*" },
    ]
  })
}

########################################
# Security group for Lambda ENIs (egress only)
########################################

resource "aws_security_group" "lambda" {
  name        = "${local.prefix}-mcp-lambda-sg"
  description = "MCP Lambda egress"
  vpc_id      = var.vpc_id
  egress {
    description = "Allow all outbound (AWS APIs, Bedrock, S3) via NAT"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.tags, { Name = "${local.prefix}-mcp-lambda-sg" })
}

########################################
# Vendor boto3>=1.40 into memory-mcp (the Lambda runtime's bundled boto3
# predates the s3vectors client). Pure-Python packages, so a local pip install
# produces a Lambda-compatible layout. Re-runs when the code or reqs change.
########################################

resource "null_resource" "vendor_memory_mcp" {
  triggers = {
    reqs = filemd5("${var.mcp_source_root}/memory-mcp/requirements.txt")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      cd "${var.mcp_source_root}/memory-mcp"
      rm -rf _vendor
      pip3 install -r requirements.txt -t _vendor --quiet --no-compile
      find _vendor -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
      find _vendor -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
    EOT
  }
}

########################################
# Sync the shared VTON prompt-engineering package into tryon-mcp's build context.
# Canonical copy: components/shared/vton_engines (the per-consumer copies are
# gitignored), so a fresh clone has nothing to zip and the deployed function
# raises ImportError on `from vton_engines import build_request`. The deploy CLI
# syncs in `storeai up`; doing it here keeps a direct `terraform apply` correct
# too. Re-runs when the canonical package changes.
########################################

resource "null_resource" "sync_vton_engines" {
  triggers = {
    engines = sha1(join("", [
      for f in sort(tolist(fileset("${var.mcp_source_root}/../shared/vton_engines", "*.py"))) :
      filemd5("${var.mcp_source_root}/../shared/vton_engines/${f}")
    ]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      mkdir -p "${var.mcp_source_root}/tryon-mcp/vton_engines"
      cp -f "${var.mcp_source_root}/../shared/vton_engines"/*.py "${var.mcp_source_root}/tryon-mcp/vton_engines/"
    EOT
  }
}

########################################
# Package + deploy each function
########################################

data "archive_file" "mcp" {
  for_each    = local.functions
  type        = "zip"
  source_dir  = "${var.mcp_source_root}/${each.key}"
  output_path = "${path.module}/build/${each.key}.zip"

  # memory-mcp needs its vendored boto3 present before zipping; tryon-mcp needs
  # the shared vton_engines package present.
  depends_on = [null_resource.vendor_memory_mcp, null_resource.sync_vton_engines]
}

resource "aws_lambda_function" "mcp" {
  for_each         = local.functions
  function_name    = "${local.prefix}-${each.key}"
  runtime          = "python3.12"
  architectures    = ["arm64"] # Graviton: pure-Python MCP tools (no native deps) — cheaper + matches the arm64 EKS services
  handler          = "lambda_function.lambda_handler"
  role             = aws_iam_role.mcp.arn
  timeout          = each.value.timeout
  memory_size      = 256
  filename         = data.archive_file.mcp[each.key].output_path
  source_code_hash = data.archive_file.mcp[each.key].output_base64sha256

  environment {
    variables = each.value.env
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  tags = local.tags

  depends_on = [aws_iam_role_policy_attachment.basic, aws_iam_role_policy_attachment.vpc]
}
