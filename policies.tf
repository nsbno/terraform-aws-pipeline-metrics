data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      identifiers = ["lambda.amazonaws.com"]
      type        = "Service"
    }
  }
}

# TODO: Scope this down?
data "aws_iam_policy_document" "cloudwatch_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "step_functions_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["states:ListExecutions"]
    resources = var.state_machine_arns
  }

  statement {
    effect    = "Allow"
    actions   = ["states:GetExecutionHistory"]
    resources = formatlist("arn:aws:states:${local.current_region}:${local.current_account_id}:execution:%s:*", local.state_machine_names)
  }
}

data "aws_iam_policy_document" "dynamodb_for_lambda" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchWriteItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem"
    ]
    resources = [aws_dynamodb_table.metrics.arn]
  }
}

data "aws_iam_policy_document" "s3_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["s3:Get*", "s3:List*", "s3:Put*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
  }
}


data "aws_iam_policy_document" "logs_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup"]
    resources = ["arn:aws:logs:${local.current_region}:${local.current_account_id}:*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      "arn:aws:logs:${local.current_region}:${local.current_account_id}:log-group:/aws/lambda/${aws_lambda_function.this.function_name}*",
    ]
  }
}
