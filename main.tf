terraform {
  required_version = ">= 1.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.9.0"
    }
  }
}

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
  arn  = "arn:aws:events:eu-west-1:${var.central_account}:event-bus/cross-account-events"
  rule = aws_cloudwatch_event_rule.sfn_status.name
}
