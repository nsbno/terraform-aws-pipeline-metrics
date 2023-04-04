terraform {
  required_version = ">= 1.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.9.0"
    }
  }
}

locals {
  eventbus_arn = "arn:aws:events:eu-west-1:${var.central_account}:event-bus/cross-account-events"
}

/*
 * == Outgoing Messages
 *
 * A way for the deployment agents to figure out what has changed.
 */
data "aws_iam_policy_document" "eventbridge_in_org_assume" {
  statement {
    effect = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eventbridge_send_cross_account" {
  name = "deployment-eventbridge-cross-account-role"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_in_org_assume.json
}

data "aws_iam_policy_document" "eventbridge_send_message_to_eventbridge" {
  statement {
    effect = "Allow"
    actions = ["events:PutEvents"]

    resources = [local.eventbus_arn]
  }
}

resource "aws_iam_role_policy" "eventbridge_send_message_to_eventbridge" {
  role   = aws_iam_role.eventbridge_send_cross_account.id
  policy = data.aws_iam_policy_document.eventbridge_send_message_to_eventbridge.json
}

/*
 * === Configuration of eventbridge rules
 */
resource "aws_cloudwatch_event_rule" "sfn_status" {
  description   = "Triggers when a State Machine changes status"
  event_pattern = <<-EOF
    {
      "source": [
        "aws.states"
      ],
      "detail-type": [
        "Step Functions Execution Status Change"
      ],
      "detail": {
        "status": ["RUNNING", "FAILED", "SUCCEEDED", "TIMED_OUT", "ABORTED"],
        "stateMachineArn": [
          {
            "prefix": ""
          }
        ]
      }
    }
  EOF
}

resource "aws_cloudwatch_event_target" "sfn_events" {
  arn  = local.eventbus_arn
  rule = aws_cloudwatch_event_rule.sfn_status.name

  role_arn = aws_iam_role.eventbridge_send_cross_account.arn
}
