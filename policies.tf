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

# TODO: Scope this down?
data "aws_iam_policy_document" "step_functions_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["states:GetExecutionHistory", "states:ListExecutions"]
    resources = ["*"]
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
