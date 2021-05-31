variable "name_prefix" {
  description = "A prefix used for naming resources."
  type        = string
}

variable "state_machine_arns" {
  description = "An optional list of ARNs of state machines to report metrics on. If empty, metrics will be collected for all state machines in the current AWS region."
  default     = []
  type        = list(string)
}

variable "create_cloudwatch_dashboard" {
  description = "Whether to create a CloudWatch dashboard per state machine or not."
  default     = false
  type        = bool
}

variable "states_to_display" {
  description = "Names of states to display in the CloudWatch dashboards."
  default     = []
  type        = list(string)
}

variable "collect_realtime_metrics" {
  description = "Whether to collect realtime metrics or not."
  default     = true
}

variable "tags" {
  description = "A map of tags (key-value pairs) passed to resources."
  type        = map(string)
  default     = {}
}

variable "lambda_timeout" {
  description = "The maximum number of seconds the Lambda is allowed to run"
  default     = 300
}

variable "lambda_log_retention_in_days" {
  description = "The number of days to retain CloudWatch logs from the Lambda."
  default     = 14
}

variable "schedule_expression" {
  description = "The schedule expression (in UTC) to use for the CloudWatch Event Rule that triggers the Lambda."
  default     = "rate(1 hour)"
}
