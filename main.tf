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
  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets = concat(
      [{
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = "\nLead Time (LT) | Change Failure Rate (CFR) | Deployment Frequency (DF) | Mean Time to Recovery (MTTR) | Run Time (RT) \n----|-----|----|-----|-----\nPipeline execution time for executions that are successful | Percentage of times a given state has failed | Number of times a given state has been successful | Time it takes for a given state to go from failure to success | Time it takes for a given state to successfully complete\n"
        }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 24
          height = 3
          properties = {
            metrics = [
              [{ expression = "m2/(60*1000)", label = "(minutes) Lead Time", id = "e2" }],
              [local.metric_namespace, "StateMachineSuccess", "StateMachineName", each.key, { id = "m2", visible = false }],
            ]
            view   = "singleValue"
            region = local.current_region
            stat   = "Average"
            period = 86400
            title  = "Overall Pipeline Metrics"
          }
        }
      ],
      flatten([for state in var.states_to_display : [
        {
          type   = "text"
          x      = 0
          y      = 0
          width  = 24
          height = 2
          properties = {
            markdown = "&nbsp;\n# **${state}**"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 24
          height = 3
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m3", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m2", stat = "Average", visible = false, label = "StateSuccessTime" }],
              [{ expression = "m2/(60*1000)", label = "(minutes) Run Time", id = "e4" }],
              [{ expression = "m3/(PERIOD(m3)/(3600*24))", id = "e2", label = "(#) Deployment Frequency" }],
              [{ expression = "100*(m4/(m3+m4))", id = "e1", label = "(%) Change Failure Rate" }],
              [{ expression = "m1/(60*1000)", label = "(minutes) Mean Time to Recovery", id = "e3" }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "DEFAULT", { label = "Other failures", id = "m4", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "TERRAFORM_LOCK", { label = "Terraform lock failures", id = "m5", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateRecovery", "StateMachineName", each.key, "StateName", state, { id = "m1", label = "StateRecovery", visible = false }]
            ]
            view                 = "singleValue"
            region               = local.current_region
            stat                 = "Average"
            period               = 604800 # Set to large value to avoid incorrect values appearing when auto-refresh is enabled for the CloudWatch Dashboard
            title                = "Key Numbers (avg. daily)"
            setPeriodToTimeRange = true
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [{ expression = "m4/(1000*60)", id = "e1", label = "Run Time" }],
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Average"
            period   = 86400
            title    = "Run Time"
            yAxis = {
              left = {
                showUnits = false
                min       = 0
                label     = "Minutes"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 6
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { stat = "SampleCount", id = "m3", label = "Deployment Frequency" }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Sum"
            period   = 86400
            title    = "Deployment Frequency"
            yAxis = {
              left = {
                min       = 0
                showUnits = false
                label     = "Frequency"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 12
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m3", stat = "SampleCount", label = "(#) Deployment frequency", visible = false }],
              [{ expression = "100*(m4/(m3+m4))", id = "e1", label = "Change Failure Rate" }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "DEFAULT", { stat = "SampleCount", label = "Other failures", id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Sum"
            period   = 86400
            title    = "Change Failure Rate"
            yAxis = {
              left = {
                min       = 0
                max       = 100
                showUnits = false
                label     = "Percentage"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 18
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [{ expression = "m4/(1000*60)", id = "e1", label = "Mean Time to Recovery" }],
              [local.metric_namespace, "StateRecovery", "StateMachineName", each.key, "StateName", state, { id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Average"
            period   = 86400
            title    = "Mean Time to Recovery"
            yAxis = {
              left = {
                showUnits = false
                min       = 0
                label     = "Minutes"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        }
        ]
      ])
    )
  })
  # TODO: Add lifecycle rule ignoring changes to dashboard
}
