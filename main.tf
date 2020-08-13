data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  current_account_id  = data.aws_caller_identity.current.account_id
  current_region      = data.aws_region.current.name
  state_machine_names = sort([for arn in var.state_machine_arns : split(":", arn)[6]])
  metric_namespace    = "${var.name_prefix}-pipeline-metrics"
}

data "archive_file" "this" {
  type        = "zip"
  source_file = "${path.module}/src/main.py"
  output_path = "${path.module}/src/main.zip"
}

resource "aws_dynamodb_table" "this" {
  name         = "${var.name_prefix}-pipeline-metrics"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "execution"
  range_key    = "metric"

  attribute {
    name = "execution"
    type = "S"
  }

  attribute {
    name = "metric"
    type = "S"
  }
  ttl {
    enabled        = true
    attribute_name = "time_to_live"
  }
  tags = var.tags
}

resource "aws_s3_bucket" "this" {
  bucket = "${local.current_account_id}-${var.name_prefix}-sfn-executions"
  versioning {
    enabled = true
  }
  tags = var.tags
}

resource "aws_lambda_function" "this" {
  function_name    = "${var.name_prefix}-pipeline-metrics"
  handler          = "main.lambda_handler"
  role             = aws_iam_role.this.arn
  runtime          = "python3.7"
  filename         = data.archive_file.this.output_path
  source_code_hash = filebase64sha256(data.archive_file.this.output_path)
  environment {
    variables = {
      CURRENT_ACCOUNT_ID  = local.current_account_id
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.this.name
      METRIC_NAMESPACE    = local.metric_namespace
      S3_BUCKET           = aws_s3_bucket.this.id
      STATE_MACHINE_ARNS  = jsonencode(var.state_machine_arns)
    }
  }
  timeout = var.lambda_timeout
  tags    = var.tags
}

resource "aws_iam_role" "this" {
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "logs_to_lambda" {
  policy = data.aws_iam_policy_document.logs_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_iam_role_policy" "cloudwatch_to_lambda" {
  policy = data.aws_iam_policy_document.cloudwatch_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_iam_role_policy" "dynamodb_to_lambda" {
  policy = data.aws_iam_policy_document.dynamodb_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_iam_role_policy" "s3_to_lambda" {
  policy = data.aws_iam_policy_document.s3_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_iam_role_policy" "step_functions_to_lambda" {
  policy = data.aws_iam_policy_document.step_functions_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_cloudwatch_event_rule" "this" {
  description         = "Invoke the metric Lambda on a schedule"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "this" {
  arn  = aws_lambda_function.this.arn
  rule = aws_cloudwatch_event_rule.this.name
}

resource "aws_lambda_permission" "this" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.this.arn
}

resource "aws_cloudwatch_dashboard" "this" {
  for_each       = toset(local.state_machine_names)
  dashboard_name = "${var.name_prefix}-${each.key}-pipeline-metrics"
  dashboard_body = local.dashboard_body
  # TODO Add lifecycle rule ignoring changes to dashboard
}
