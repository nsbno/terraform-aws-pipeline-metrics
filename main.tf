data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  current_account_id  = data.aws_caller_identity.current.account_id
  current_region      = data.aws_region.current.name
  state_machine_names = sort([for arn in var.state_machine_arns : split(":", arn)[6]])
  metric_namespace    = "${var.name_prefix}Pipeline"
  metric_dimension    = "PipelineName"
}

data "archive_file" "this" {
  type        = "zip"
  source_file = "${path.module}/src/main.py"
  output_path = "${path.module}/src/main.zip"
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
      STATE_NAMES      = jsonencode(var.state_names)
      METRIC_NAMESPACE = local.metric_namespace
      METRIC_DIMENSION = local.metric_dimension
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

resource "aws_iam_role_policy" "ssm_to_lambda" {
  policy = data.aws_iam_policy_document.ssm_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_iam_role_policy" "step_functions_to_lambda" {
  policy = data.aws_iam_policy_document.step_functions_for_lambda.json
  role   = aws_iam_role.this.id
}

resource "aws_cloudwatch_event_rule" "this" {
  description   = "Update CloudWatch metrics on Step Functions Execution Status Change"
  event_pattern = <<EOF
{
  "source": [
    "aws.states"
  ],
  "detail-type": [
    "Step Functions Execution Status Change"
  ],
  "detail": {
    "status": ["SUCCEEDED", "FAILED"],
    "stateMachineArn": ${jsonencode(var.state_machine_arns)}
  }
}
EOF
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
  dashboard_name = "${var.name_prefix}-pipeline-metrics"
  dashboard_body = jsonencode({
    widgets = concat(
      [{
        type   = "text"
        x      = 0
        y      = 0
        width  = 18
        height = 3
        properties = {
          markdown = "\nChange Failure Rate | Deployment Frequency | Lead Time | Mean Time to Recovery\n----|-----|----|-----\nPercentage of failed deployments to production | Frequency of  successful deployments to production | Pipeline execution time for executions that successfully deploy to production | Time it takes to go from a failed to a successful deployment to production\n"
        }
      }],
      [for pipeline_name in local.state_machine_names : {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 18
        height = 3
        properties = {
          metrics = [
            [local.metric_namespace, "SuccessfulDeploymentToProduction", local.metric_dimension, pipeline_name, { id = "m3", label = "(#) Deployment frequency", stat = "Sum" }],
            [{ expression = "100*(m4/(m3+m4))", id = "e1", label = "(%) Change failure rate" }],
            [{ expression = "FLOOR(m2/(60*1000))", label = "(minutes) Lead time", id = "e2" }],
            [{ expression = "FLOOR(m1/(60*1000))", label = "(minutes) Mean time to recovery", id = "e3" }],
            [local.metric_namespace, "ExecutionTime", local.metric_dimension, pipeline_name, { id = "m2", label = "ExecutionTime", visible = false }],
            [local.metric_namespace, "FailedDeploymentToProduction", local.metric_dimension, pipeline_name, { id = "m4", stat = "Sum", visible = false }],
            [local.metric_namespace, "MeanTimeToRecovery", local.metric_dimension, pipeline_name, { id = "m1", label = "MeanTimeToRecovery", visible = false }]
          ]
          view    = "singleValue"
          region  = local.current_region
          stat    = "Average"
          period  = 86400
          title   = pipeline_name
          stacked = false
        }
        }
      ]
    )
  })
}
