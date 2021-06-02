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
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.metric_namespace, local.metric_namespace_realtime]
    }
  }
}

data "aws_iam_policy_document" "step_functions_for_lambda" {
  statement {
    effect    = "Allow"
    actions   = ["states:ListExecutions"]
    resources = length(var.state_machine_arns) > 0 ? var.state_machine_arns : ["arn:aws:states:${local.current_region}:${local.current_account_id}:stateMachine:*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["states:ListStateMachines"]
    resources = ["*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["states:GetExecutionHistory"]
    resources = length(local.state_machine_names) > 0 ? formatlist("arn:aws:states:${local.current_region}:${local.current_account_id}:execution:%s:*", local.state_machine_names) : ["arn:aws:states:${local.current_region}:${local.current_account_id}:execution:*"]
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
    resources = [aws_dynamodb_table.this.arn]
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
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = [
      "${aws_cloudwatch_log_group.this.arn}:*",
      "${aws_cloudwatch_log_group.realtime.arn}:*",
    ]
  }
}
